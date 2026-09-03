import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import MethodType

import pytest
from fragment_api import (
    AvailabilityCheck,
    ConflictError,
    FragmentAPIError,
    PriceQuote,
    Prices,
    Purchase,
    RateLimitError,
    ServiceUnavailableError,
    ValidationError,
)

from stars_market_bot.db import OrderRecord
from stars_market_bot.domain import Asset, OrderState, Product
import stars_market_bot.fragment as fragment_module
from stars_market_bot.fragment import (
    FragmentClient,
    FragmentPermanentError,
    FragmentTemporaryError,
)


NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def adapter_traceback_frames(error: BaseException):
    module_path = Path(fragment_module.__file__).resolve()
    traceback = error.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        if Path(frame.f_code.co_filename).resolve() == module_path:
            yield frame
        traceback = traceback.tb_next


def retains_target(value, targets, *, seen=None, depth=0):
    if any(value is target for target in targets):
        return True
    if isinstance(value, str):
        return any(isinstance(target, str) and value == target for target in targets)
    if depth >= 8 or value is None or isinstance(
        value,
        (bytes, int, float, bool, type),
    ):
        return False

    seen = set() if seen is None else seen
    identity = id(value)
    if identity in seen:
        return False
    seen.add(identity)

    if isinstance(value, dict):
        children = (*value.keys(), *value.values())
    elif isinstance(value, (list, tuple, set, frozenset)):
        children = value
    elif isinstance(value, MethodType):
        children = (value.__self__,)
    elif isinstance(value, BaseException):
        children = (
            value.args,
            vars(value),
            value.__context__,
            value.__cause__,
        )
    elif hasattr(value, "__dict__"):
        children = tuple(vars(value).values())
    else:
        return False
    return any(
        retains_target(child, targets, seen=seen, depth=depth + 1)
        for child in children
    )


def order_fixture(
    *,
    product: Product = Product.STARS,
    recipient: str = "recipient",
    amount: int = 100,
    asset: Asset = Asset.GRAM,
    idempotency_key: str = "order-41",
) -> OrderRecord:
    return OrderRecord(
        id=41,
        user_id=1001,
        product=product,
        recipient=recipient,
        product_amount=amount if product is Product.STARS else None,
        months=amount if product is Product.PREMIUM else None,
        asset=asset,
        quoted_api_units=1,
        customer_units=None,
        destination=None,
        reference=None,
        invoice_created_at=None,
        created_at=NOW,
        updated_at=NOW,
        expires_at=None,
        state=OrderState.PAID,
        idempotency_key=idempotency_key,
        fragment_purchase_id=None,
        payment_hash=None,
        final_transaction_hash=None,
        error_code=None,
        error_message=None,
    )


class FakeFragmentAPI:
    def __init__(
        self,
        *,
        error: BaseException | None = None,
        purchase_status: str = "queued",
        purchase_error: dict | None = None,
    ) -> None:
        self.error = error
        self.purchase_status = purchase_status
        self.purchase_error = purchase_error
        self.check_calls: list[dict[str, object]] = []
        self.quote_calls: list[dict[str, object]] = []
        self.create_calls: list[dict[str, object]] = []
        self.status_calls: list[str] = []
        self.wait_calls = 0

    def _raise_if_needed(self) -> None:
        if self.error is not None:
            raise self.error

    def check_availability(self, **kwargs):
        self.check_calls.append(kwargs)
        self._raise_if_needed()
        return AvailabilityCheck(
            available=True,
            code="AVAILABLE",
            message="The purchase can be created.",
            checked_at="2026-09-03T12:00:00Z",
        )

    def get_price(self, product, amount):
        self.quote_calls.append({"product": product, "amount": amount})
        self._raise_if_needed()
        return PriceQuote(
            product=product,
            amount=amount,
            prices=Prices(gram="1.25", usdt="3.75"),
            stale=False,
        )

    def create_purchase(self, **kwargs):
        self.create_calls.append(kwargs)
        self._raise_if_needed()
        return Purchase(
            purchase_id="purchase-9",
            status=self.purchase_status,
            error=self.purchase_error,
        )

    def get_purchase(self, purchase_id):
        self.status_calls.append(purchase_id)
        self._raise_if_needed()
        return Purchase(
            purchase_id=purchase_id,
            status=self.purchase_status,
            error=self.purchase_error,
        )

    def wait(self, *args, **kwargs):
        self.wait_calls += 1
        raise AssertionError("the adapter must not call SDK wait")


def test_create_reuses_local_idempotency_key():
    fake = FakeFragmentAPI()
    client = FragmentClient(fake)

    result = asyncio.run(
        client.create(
            order_fixture(idempotency_key="order-41"),
            "seed",
            "UQwallet",
        )
    )

    assert result.purchase_id == "purchase-9"
    assert fake.create_calls[0]["idempotency_key"] == "order-41"


def test_failed_purchase_preserves_safe_top_up_code():
    fake = FakeFragmentAPI(
        purchase_status="failed",
        purchase_error={"code": "TOP_UP_REQUIRED", "message": "private detail"},
    )

    result = asyncio.run(
        FragmentClient(fake).create(order_fixture(), "seed", "UQwallet")
    )

    assert result.error_code == "TOP_UP_REQUIRED"


def test_rate_limit_becomes_temporary_error():
    fake = FakeFragmentAPI(
        error=RateLimitError(
            "slow",
            status_code=429,
            response={"retry_after": 4},
        )
    )

    with pytest.raises(FragmentTemporaryError) as raised:
        asyncio.run(FragmentClient(fake).quote(Product.STARS, 100))

    assert raised.value.retry_after == 4


def test_all_sdk_methods_run_through_to_thread(monkeypatch):
    calls: list[str] = []

    async def recording_to_thread(function, /, *args, **kwargs):
        calls.append(function.__name__)
        return function(*args, **kwargs)

    monkeypatch.setattr(
        "stars_market_bot.fragment.asyncio.to_thread",
        recording_to_thread,
    )
    fake = FakeFragmentAPI()
    client = FragmentClient(fake)

    async def scenario():
        await client.check(Product.STARS, "recipient", 100, Asset.GRAM)
        await client.quote(Product.STARS, 100)
        await client.create(order_fixture(), "seed", "UQwallet")
        await client.status("purchase-9")

    asyncio.run(scenario())

    assert calls == [
        "check_availability",
        "get_price",
        "create_purchase",
        "get_purchase",
    ]


@pytest.mark.parametrize(
    ("product", "amount", "asset"),
    [
        (Product.STARS, 250, Asset.GRAM),
        (Product.PREMIUM, 6, Asset.USDT),
    ],
)
def test_check_maps_domain_arguments_exactly(product, amount, asset):
    fake = FakeFragmentAPI()

    asyncio.run(FragmentClient(fake).check(product, "recipient", amount, asset))

    assert fake.check_calls == [
        {
            "product": product.value,
            "username": "@recipient",
            "amount": amount,
            "payment_method": asset.value,
        }
    ]


@pytest.mark.parametrize(
    ("product", "amount"),
    [(Product.STARS, 250), (Product.PREMIUM, 12)],
)
def test_quote_maps_product_and_amount_exactly(product, amount):
    fake = FakeFragmentAPI()

    asyncio.run(FragmentClient(fake).quote(product, amount))

    assert fake.quote_calls == [{"product": product.value, "amount": amount}]


@pytest.mark.parametrize(
    ("product", "amount", "asset"),
    [
        (Product.STARS, 500, Asset.USDT),
        (Product.PREMIUM, 3, Asset.GRAM),
    ],
)
def test_create_maps_order_and_pinned_wallet_exactly(product, amount, asset):
    fake = FakeFragmentAPI()
    order = order_fixture(product=product, amount=amount, asset=asset)

    asyncio.run(FragmentClient(fake).create(order, "owner seed", "UQpinned"))

    assert fake.create_calls == [
        {
            "product": product.value,
            "username": "@recipient",
            "amount": amount,
            "payment_method": asset.value,
            "seed": "owner seed",
            "idempotency_key": "order-41",
            "wallet_address": "UQpinned",
        }
    ]


@pytest.mark.parametrize("recipient", ["recipient", "@recipient"])
def test_username_boundary_has_exactly_one_at_prefix(recipient):
    fake = FakeFragmentAPI()

    asyncio.run(
        FragmentClient(fake).check(
            Product.STARS,
            recipient,
            100,
            Asset.GRAM,
        )
    )

    assert fake.check_calls[0]["username"] == "@recipient"


def test_username_boundary_does_not_weaken_domain_validation():
    fake = FakeFragmentAPI()

    with pytest.raises(FragmentPermanentError):
        asyncio.run(
            FragmentClient(fake).check(
                Product.STARS,
                "@@recipient",
                100,
                Asset.GRAM,
            )
        )

    assert fake.check_calls == []


@pytest.mark.parametrize(
    "error_factory",
    [
        lambda: ServiceUnavailableError("unsafe", status_code=503),
        lambda: FragmentAPIError("unsafe", status_code=500),
        lambda: FragmentAPIError("unsafe", status_code=502),
        lambda: FragmentAPIError("unsafe", status_code=503),
        lambda: FragmentAPIError("unsafe", status_code=504),
        lambda: FragmentAPIError("unsafe"),
        lambda: TimeoutError("unsafe"),
        lambda: ConnectionError("unsafe"),
        lambda: OSError("unsafe"),
    ],
)
def test_retryable_failures_become_temporary_errors(error_factory):
    fake = FakeFragmentAPI(error=error_factory())

    with pytest.raises(FragmentTemporaryError):
        asyncio.run(FragmentClient(fake).status("purchase-9"))


def test_generic_429_preserves_positive_retry_after():
    fake = FakeFragmentAPI(
        error=FragmentAPIError(
            "unsafe",
            status_code=429,
            response={"retry_after": 7},
        )
    )

    with pytest.raises(FragmentTemporaryError) as raised:
        asyncio.run(FragmentClient(fake).quote(Product.STARS, 100))

    assert raised.value.retry_after == 7


@pytest.mark.parametrize(
    "error_factory",
    [
        lambda: ValidationError("unsafe", status_code=400),
        lambda: ConflictError("unsafe", status_code=409),
        lambda: FragmentAPIError("unsafe", status_code=400),
        lambda: FragmentAPIError("unsafe", status_code=404),
    ],
)
def test_non_retryable_failures_become_permanent_errors(error_factory):
    fake = FakeFragmentAPI(error=error_factory())

    with pytest.raises(FragmentPermanentError):
        asyncio.run(FragmentClient(fake).quote(Product.PREMIUM, 3))


def test_safe_error_metadata_is_preserved():
    fake = FakeFragmentAPI(
        error=ValidationError(
            "unsafe upstream detail",
            status_code=400,
            response={
                "code": "INVALID_BIP39_SEED",
                "action": "Check all 12 words and their order",
            },
        )
    )

    with pytest.raises(FragmentPermanentError) as raised:
        asyncio.run(FragmentClient(fake).quote(Product.STARS, 100))

    assert raised.value.code == "INVALID_BIP39_SEED"
    assert raised.value.action == "Check all 12 words and their order"


def test_translated_error_exposes_no_secrets_or_upstream_payload():
    seed = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu"
    wallet = "UQprivatewallet"
    uppercase_secret = "TOPSECRETRECOVERYVALUE"
    payload = {"seed": seed, "wallet_address": wallet}
    fake = FakeFragmentAPI(
        error=ValidationError(
            f"request failed: {payload!r}",
            status_code=400,
            response={
                "code": uppercase_secret,
                "action": f"Retry with {seed}",
                "request": payload,
            },
        )
    )

    with pytest.raises(FragmentPermanentError) as raised:
        asyncio.run(FragmentClient(fake).quote(Product.STARS, 100))

    error = raised.value
    public_values = (str(error), error.code, error.action, error.public_message)
    rendered = " ".join(value for value in public_values if value is not None)
    assert seed not in rendered
    assert wallet not in rendered
    assert uppercase_secret not in rendered
    assert "wallet_address" not in rendered
    assert repr(payload) not in rendered
    assert not hasattr(error, "response")


@pytest.mark.parametrize(
    ("upstream_factory", "translated_type"),
    [
        (
            lambda: ValidationError(
                "unsafe SDK detail",
                status_code=400,
                response={"seed": "TEST-ONLY-SDK-SECRET"},
            ),
            FragmentPermanentError,
        ),
        (
            lambda: TimeoutError("TEST-ONLY-TRANSPORT-SECRET"),
            FragmentTemporaryError,
        ),
    ],
)
def test_translated_errors_discard_upstream_exception_chain(
    upstream_factory,
    translated_type,
):
    upstream = upstream_factory()
    fake = FakeFragmentAPI(error=upstream)

    with pytest.raises(translated_type) as raised:
        asyncio.run(FragmentClient(fake).quote(Product.STARS, 100))

    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None


@pytest.mark.parametrize(
    ("failure_kind", "translated_type"),
    [
        ("sdk", FragmentPermanentError),
        ("transport", FragmentTemporaryError),
    ],
)
def test_create_error_traceback_discards_all_sensitive_references(
    failure_kind,
    translated_type,
):
    seed = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu"
    wallet = "UQ-round-two-private-wallet"
    response = {
        "seed": seed,
        "wallet_address": wallet,
        "body": {"private": "TEST-ONLY-PAYLOAD"},
    }
    if failure_kind == "sdk":
        upstream = ValidationError(
            "unsafe SDK detail",
            status_code=400,
            response=response,
        )
    else:
        upstream = TimeoutError(f"transport failed for {seed} and {wallet}")
        upstream.response = response
    fake = FakeFragmentAPI(error=upstream)

    with pytest.raises(translated_type) as raised:
        asyncio.run(
            FragmentClient(fake).create(
                order_fixture(),
                seed,
                wallet,
            )
        )

    error = raised.value
    assert error.__context__ is None
    assert error.__cause__ is None
    frames = list(adapter_traceback_frames(error))
    assert frames
    targets = (seed, wallet, upstream, response)
    for frame in frames:
        retaining_locals = {
            name
            for name, value in frame.f_locals.items()
            if retains_target(value, targets)
        }
        assert retaining_locals == set(), (
            f"adapter frame {frame.f_code.co_name} retains sensitive locals: "
            f"{sorted(retaining_locals)}"
        )


def test_cancellation_is_preserved():
    fake = FakeFragmentAPI(error=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(FragmentClient(fake).status("purchase-9"))


@pytest.mark.parametrize(
    ("status", "terminal", "completed"),
    [
        ("queued", False, False),
        ("preparing", False, False),
        ("sending", False, False),
        ("submitted", False, False),
        ("confirming", False, False),
        ("completed", True, True),
        ("failed", True, False),
        ("reconciliation_required", True, False),
    ],
)
def test_purchase_statuses_are_preserved(status, terminal, completed):
    fake = FakeFragmentAPI(purchase_status=status)

    result = asyncio.run(FragmentClient(fake).status("purchase-9"))

    assert result.status == status
    assert result.terminal is terminal
    assert result.completed is completed


def test_unknown_purchase_status_fails_closed():
    fake = FakeFragmentAPI(purchase_status="mystery")

    with pytest.raises(FragmentPermanentError) as raised:
        asyncio.run(FragmentClient(fake).status("purchase-9"))

    assert raised.value.code == "INVALID_RESPONSE"


def test_malformed_availability_response_fails_closed():
    class MalformedAvailabilityAPI(FakeFragmentAPI):
        def check_availability(self, **kwargs):
            self.check_calls.append(kwargs)
            return {"available": True, "payload": "unsafe"}

    with pytest.raises(FragmentPermanentError) as raised:
        asyncio.run(
            FragmentClient(MalformedAvailabilityAPI()).check(
                Product.STARS,
                "recipient",
                100,
                Asset.GRAM,
            )
        )

    assert raised.value.code == "INVALID_RESPONSE"
    assert "unsafe" not in str(raised.value)


def test_malformed_quote_response_fails_closed():
    class MalformedQuoteAPI(FakeFragmentAPI):
        def get_price(self, product, amount):
            self.quote_calls.append({"product": product, "amount": amount})
            return {"product": product, "amount": amount, "payload": "unsafe"}

    with pytest.raises(FragmentPermanentError) as raised:
        asyncio.run(FragmentClient(MalformedQuoteAPI()).quote(Product.STARS, 100))

    assert raised.value.code == "INVALID_RESPONSE"
    assert "unsafe" not in str(raised.value)


def test_create_does_not_wait_retry_or_duplicate_after_temporary_failure():
    fake = FakeFragmentAPI(
        error=ServiceUnavailableError("unsafe", status_code=503)
    )

    with pytest.raises(FragmentTemporaryError):
        asyncio.run(
            FragmentClient(fake).create(order_fixture(), "seed", "UQwallet")
        )

    assert len(fake.create_calls) == 1
    assert fake.wait_calls == 0
