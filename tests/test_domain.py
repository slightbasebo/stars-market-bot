from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from urllib.parse import parse_qs, urlparse

import pytest

import stars_market_bot.domain as domain
from stars_market_bot.domain import (
    CANONICAL_USDT_MASTER,
    Asset,
    Invoice,
    MatchResult,
    Money,
    OrderState,
    PaymentCandidate,
    Product,
    build_payment_link,
    can_transition,
    match_payment,
    normalize_username,
    quote_customer_amount,
)


NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
EXPIRY = NOW + timedelta(minutes=15)


def invoice_fixture(
    *,
    asset: Asset = Asset.GRAM,
    units: int = 2_000_000_000,
    destination: str = "UQ-store-wallet",
    reference: str = "SM-7K4Q2P",
    created_at: datetime = NOW,
    expires_at: datetime = EXPIRY,
) -> Invoice:
    return Invoice(
        destination=destination,
        asset=asset,
        units=units,
        reference=reference,
        created_at=created_at,
        expires_at=expires_at,
    )


def candidate_fixture(
    *,
    asset: Asset = Asset.GRAM,
    units: int = 2_000_000_000,
    destination: str = "UQ-store-wallet",
    comment: str | None = "SM-7K4Q2P",
    timestamp: datetime = NOW + timedelta(minutes=1),
    finalized: bool = True,
    aborted: bool = False,
    bounced: bool = False,
    jetton_master: str | None = None,
) -> PaymentCandidate:
    return PaymentCandidate(
        tx_hash="tx-1",
        logical_time=41,
        destination=destination,
        asset=asset,
        units=units,
        comment=comment,
        timestamp=timestamp,
        finalized=finalized,
        aborted=aborted,
        bounced=bounced,
        jetton_master=jetton_master,
    )


def test_required_enums_expose_stable_storage_values():
    assert Asset.GRAM.value == "gram"
    assert Asset.USDT.value == "usdt"
    assert Product.STARS.value == "stars"
    assert Product.PREMIUM.value == "premium"
    assert OrderState.RECONCILIATION_REQUIRED.value == "reconciliation_required"
    assert MatchResult.WRONG_REFERENCE.value == "wrong_reference"


def test_usdt_quote_rounds_up_to_six_decimals():
    money = quote_customer_amount("1.000001", Decimal("10"), Asset.USDT)

    assert money == Money(asset=Asset.USDT, units=1_100_002)


def test_quote_preserves_fraction_beyond_ambient_decimal_precision():
    money = quote_customer_amount(
        "1.0000000000000000000000000001", Decimal("0"), Asset.GRAM
    )

    assert money.units == 1_000_000_001


def test_quote_is_independent_of_low_ambient_decimal_precision():
    with localcontext() as context:
        context.prec = 6

        money = quote_customer_amount("1.000001", Decimal("10"), Asset.USDT)

        assert money.units == 1_100_002
        assert context.prec == 6


@pytest.mark.parametrize(
    ("api_amount", "commission", "asset", "expected_units"),
    [
        ("1", Decimal("10"), Asset.GRAM, 1_100_000_000),
        ("0.000000001", Decimal("10"), Asset.GRAM, 2),
        ("1", Decimal("10"), Asset.USDT, 1_100_000),
        ("0.000001", Decimal("10"), Asset.USDT, 2),
        ("3.1415926535", Decimal("0"), Asset.GRAM, 3_141_592_654),
        ("3.1415926", Decimal("0"), Asset.USDT, 3_141_593),
    ],
)
def test_quote_uses_asset_precision_and_always_rounds_up(
    api_amount, commission, asset, expected_units
):
    assert quote_customer_amount(api_amount, commission, asset).units == expected_units


@pytest.mark.parametrize(
    ("api_amount", "commission"),
    [
        ("not-a-number", Decimal("10")),
        ("NaN", Decimal("10")),
        ("Infinity", Decimal("10")),
        ("0", Decimal("10")),
        ("-1", Decimal("10")),
        ("1", Decimal("NaN")),
        ("1", Decimal("Infinity")),
        ("1", Decimal("-0.01")),
        ("1", Decimal("100.01")),
    ],
)
def test_quote_rejects_invalid_decimal_or_commission(api_amount, commission):
    with pytest.raises(ValueError):
        quote_customer_amount(api_amount, commission, Asset.GRAM)


@pytest.mark.parametrize(
    ("api_amount", "commission"),
    [
        (1.0, Decimal("10")),
        ("1", 10.0),
    ],
)
def test_quote_rejects_float_inputs(api_amount, commission):
    with pytest.raises(TypeError):
        quote_customer_amount(api_amount, commission, Asset.GRAM)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Alice_1", "Alice_1"),
        ("@Alice_1", "Alice_1"),
        ("_name", "_name"),
        ("a" * 32, "a" * 32),
    ],
)
def test_username_normalizes_optional_leading_at(raw, expected):
    assert normalize_username(raw) == expected


@pytest.mark.parametrize(
    "value",
    [
        "abcd",
        "a" * 33,
        "1alice",
        "@1alice",
        "@@alice",
        "alice-name",
        "alice name",
        " alice",
        "алиса",
        "",
    ],
)
def test_username_rejects_malformed_values(value):
    with pytest.raises(ValueError):
        normalize_username(value)


def test_username_rejects_non_string_values():
    with pytest.raises(TypeError):
        normalize_username(123)


def test_gram_payment_link_encodes_invoice_fields_without_jetton():
    invoice = invoice_fixture(
        destination="UQ-wallet_address",
        units=1_234_567_890,
        reference="SM / 7+K?",
    )

    parsed = urlparse(build_payment_link(invoice))

    assert parsed.scheme == "ton"
    assert parsed.netloc == "transfer"
    assert parsed.path == "/UQ-wallet_address"
    assert parse_qs(parsed.query) == {
        "amount": ["1234567890"],
        "text": ["SM / 7+K?"],
        "exp": [str(int(EXPIRY.timestamp()))],
    }
    assert "%2F" in parsed.query
    assert "%2B" in parsed.query


def test_usdt_payment_link_includes_only_canonical_jetton_master():
    invoice = invoice_fixture(asset=Asset.USDT, units=1_100_002)

    query = parse_qs(urlparse(build_payment_link(invoice)).query)

    assert query["amount"] == ["1100002"]
    assert query["jetton"] == [CANONICAL_USDT_MASTER]


def test_payment_requires_exact_reference_and_units():
    invoice = invoice_fixture(
        asset=Asset.GRAM, units=2_000_000_000, reference="SM-7K4Q2P"
    )
    candidate = candidate_fixture(
        asset=Asset.GRAM, units=2_000_000_000, comment="SM-7K4Q2P"
    )

    assert match_payment(invoice, candidate) is MatchResult.MATCH
    assert (
        match_payment(invoice, replace(candidate, units=1_999_999_999))
        is MatchResult.WRONG_AMOUNT
    )


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"destination": "UQ-other-wallet"}, MatchResult.WRONG_DESTINATION),
        ({"asset": Asset.USDT}, MatchResult.WRONG_ASSET),
        ({"units": 2_000_000_001}, MatchResult.WRONG_AMOUNT),
        ({"comment": "SM-WRONG"}, MatchResult.WRONG_REFERENCE),
        ({"comment": None}, MatchResult.WRONG_REFERENCE),
        (
            {"timestamp": NOW - timedelta(microseconds=1)},
            MatchResult.OUTSIDE_INVOICE_WINDOW,
        ),
        (
            {"timestamp": EXPIRY + timedelta(microseconds=1)},
            MatchResult.OUTSIDE_INVOICE_WINDOW,
        ),
        ({"finalized": False}, MatchResult.NOT_FINAL),
        ({"aborted": True}, MatchResult.ABORTED),
        ({"bounced": True}, MatchResult.BOUNCED),
        ({"jetton_master": CANONICAL_USDT_MASTER}, MatchResult.WRONG_JETTON_MASTER),
    ],
)
def test_gram_payment_rejects_each_invalid_dimension(changes, expected):
    assert match_payment(invoice_fixture(), replace(candidate_fixture(), **changes)) is expected


def test_invoice_window_boundaries_are_inclusive():
    invoice = invoice_fixture()

    assert (
        match_payment(invoice, candidate_fixture(timestamp=invoice.created_at))
        is MatchResult.MATCH
    )
    assert (
        match_payment(invoice, candidate_fixture(timestamp=invoice.expires_at))
        is MatchResult.MATCH
    )


def test_usdt_payment_requires_canonical_master():
    invoice = invoice_fixture(asset=Asset.USDT, units=1_100_002)
    candidate = candidate_fixture(
        asset=Asset.USDT,
        units=1_100_002,
        jetton_master=CANONICAL_USDT_MASTER,
    )

    assert match_payment(invoice, candidate) is MatchResult.MATCH
    assert (
        match_payment(invoice, replace(candidate, jetton_master="EQ-fake-usdt-master"))
        is MatchResult.WRONG_JETTON_MASTER
    )
    assert (
        match_payment(invoice, replace(candidate, jetton_master=None))
        is MatchResult.WRONG_JETTON_MASTER
    )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Money(asset=Asset.GRAM, units=0),
        lambda: Money(asset=Asset.GRAM, units=True),
        lambda: Invoice(
            destination="",
            asset=Asset.GRAM,
            units=1,
            reference="SM-VALID",
            created_at=NOW,
            expires_at=EXPIRY,
        ),
        lambda: Invoice(
            destination="UQ-wallet",
            asset=Asset.GRAM,
            units=1,
            reference="",
            created_at=NOW,
            expires_at=EXPIRY,
        ),
        lambda: Invoice(
            destination="UQ-wallet",
            asset=Asset.GRAM,
            units=1,
            reference="SM-VALID",
            created_at=NOW,
            expires_at=NOW,
        ),
        lambda: PaymentCandidate(
            tx_hash="",
            logical_time=1,
            destination="UQ-wallet",
            asset=Asset.GRAM,
            units=1,
            comment="SM-VALID",
            timestamp=NOW,
            finalized=True,
            aborted=False,
            bounced=False,
        ),
        lambda: PaymentCandidate(
            tx_hash="tx-1",
            logical_time=-1,
            destination="UQ-wallet",
            asset=Asset.GRAM,
            units=1,
            comment="SM-VALID",
            timestamp=NOW,
            finalized=True,
            aborted=False,
            bounced=False,
        ),
    ],
)
def test_public_dataclasses_reject_obvious_invalid_inputs(factory):
    with pytest.raises(ValueError):
        factory()


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Invoice(
            destination="UQ-wallet",
            asset=Asset.GRAM,
            units=1,
            reference="SM-VALID",
            created_at=datetime(2026, 9, 3, 12, 0),
            expires_at=EXPIRY,
        ),
        lambda: PaymentCandidate(
            tx_hash="tx-1",
            logical_time=1,
            destination="UQ-wallet",
            asset=Asset.GRAM,
            units=1,
            comment="SM-VALID",
            timestamp=datetime(2026, 9, 3, 12, 0),
            finalized=True,
            aborted=False,
            bounced=False,
        ),
    ],
)
def test_public_dataclasses_reject_naive_datetimes(factory):
    with pytest.raises(ValueError, match="UTC"):
        factory()


def test_public_dataclasses_are_frozen():
    with pytest.raises(AttributeError):
        invoice_fixture().units = 99


ALLOWED_TRANSITIONS = {
    (OrderState.DRAFT, OrderState.AWAITING_PAYMENT),
    (OrderState.AWAITING_PAYMENT, OrderState.PAID),
    (OrderState.AWAITING_PAYMENT, OrderState.EXPIRED),
    (OrderState.AWAITING_PAYMENT, OrderState.MANUAL_REVIEW),
    (OrderState.PAID, OrderState.PURCHASING),
    (OrderState.PAID, OrderState.MANUAL_REVIEW),
    (OrderState.PURCHASING, OrderState.COMPLETED),
    (OrderState.PURCHASING, OrderState.FAILED),
    (OrderState.PURCHASING, OrderState.RECONCILIATION_REQUIRED),
    (OrderState.PURCHASING, OrderState.MANUAL_REVIEW),
    (OrderState.RECONCILIATION_REQUIRED, OrderState.COMPLETED),
    (OrderState.RECONCILIATION_REQUIRED, OrderState.FAILED),
}


@pytest.mark.parametrize(("current", "target"), sorted(ALLOWED_TRANSITIONS, key=str))
def test_every_declared_transition_is_allowed(current, target):
    assert can_transition(current, target)


def test_no_undeclared_transition_is_allowed():
    for current in OrderState:
        for target in OrderState:
            assert can_transition(current, target) is ((current, target) in ALLOWED_TRANSITIONS)


def test_reconciliation_cannot_return_to_purchasing():
    assert not can_transition(
        OrderState.RECONCILIATION_REQUIRED, OrderState.PURCHASING
    )


def test_transition_graph_is_private_and_immutable():
    assert not hasattr(domain, "TRANSITIONS")
    graph = domain._TRANSITIONS

    with pytest.raises(TypeError):
        graph[OrderState.COMPLETED] = frozenset({OrderState.PURCHASING})
    with pytest.raises(AttributeError):
        graph[OrderState.DRAFT].add(OrderState.COMPLETED)

    assert not can_transition(OrderState.COMPLETED, OrderState.PURCHASING)


@pytest.mark.parametrize(
    "terminal",
        [
            OrderState.COMPLETED,
            OrderState.FAILED,
            OrderState.MANUAL_REVIEW,
            OrderState.EXPIRED,
        ],
)
def test_terminal_and_manual_states_have_no_outgoing_transitions(terminal):
    assert not any(can_transition(terminal, target) for target in OrderState)
