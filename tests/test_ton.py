import asyncio
import base64
from datetime import datetime, timedelta, timezone

import pytest
from pytoniq import Builder

from stars_market_bot.db import Repository
from stars_market_bot.domain import (
    Asset,
    CANONICAL_USDT_MASTER,
    Invoice,
    OrderState,
    Product,
)
from stars_market_bot.ton import (
    PaymentScanner,
    ScanBatch,
    TonCenterClient,
    TonCenterTemporaryError,
    derive_wallet_addresses,
    parse_gram_transactions,
    parse_plain_comment,
    parse_usdt_transfers,
    validate_owner_wallet,
)


MNEMONIC = (
    "guilt maple grape smoke furnace gain bullet tattoo side unusual above order "
    "special life police crowd morning engine initial potato suit alpha blame surge"
)
V4_ADDRESS = "UQC_kDKawGkRsTEEdpL6pwiGJKWomVSx2Cn1s8H1SzSsXjUy"
V5_ADDRESS = "UQCRFY2ZCWj8PowktL5p689EgnwDSqNBpv_OTx9nmzUajLu_"
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def test_rate_limit_delays_requests_then_recovers(monkeypatch):
    from stars_market_bot import ton

    clock = [100.0]
    monkeypatch.setattr(ton.time, "monotonic", lambda: clock[0])

    class Response:
        def __init__(self, status):
            self.status = status
            self.headers = {"Retry-After": "12"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def raise_for_status(self):
            assert self.status == 200

        async def json(self):
            return {"accounts": [{"balance": "1000000000"}]}

    class Session:
        def __init__(self):
            self.calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            assert kwargs["timeout"].total == 15
            return Response(429 if self.calls == 1 else 200)

    async def scenario():
        session = Session()
        client = TonCenterClient(session, "https://toncenter.com/api/v3")
        with pytest.raises(TonCenterTemporaryError) as failed:
            await client.account_balance(V4_ADDRESS)
        assert failed.value.retry_after == 12
        with pytest.raises(TonCenterTemporaryError):
            await client.account_balance(V4_ADDRESS)
        assert session.calls == 1
        clock[0] += 13
        assert await client.account_balance(V4_ADDRESS) == 1_000_000_000
        assert session.calls == 2

    asyncio.run(scenario())


def comment_boc(value: str, *, opcode: int = 0) -> str:
    cell = Builder().store_uint(opcode, 32).store_snake_string(value).end_cell()
    return base64.b64encode(cell.to_boc()).decode()


def test_derives_v4r2_and_v5r1_mainnet_addresses_without_network():
    addresses = derive_wallet_addresses(MNEMONIC)

    assert addresses.v4 == V4_ADDRESS
    assert addresses.v5r1 == V5_ADDRESS
    assert validate_owner_wallet(MNEMONIC, V5_ADDRESS) == V5_ADDRESS


def test_account_balance_reads_nanograms_from_account_state():
    client = object.__new__(TonCenterClient)

    async def get(path, params):
        assert path == "/accountStates"
        assert params["address"] == V5_ADDRESS
        return {"accounts": [{"status": "active", "balance": "556264347"}]}

    client._get = get

    assert asyncio.run(client.account_balance(V5_ADDRESS)) == 556_264_347


def test_wallet_validation_rejects_invalid_seed_and_unrelated_address():
    with pytest.raises(ValueError, match="valid 24-word"):
        derive_wallet_addresses("word " * 24)
    with pytest.raises(ValueError, match="derived"):
        validate_owner_wallet(MNEMONIC, "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM9c")


def test_plain_comment_requires_zero_opcode_and_valid_utf8():
    assert parse_plain_comment(comment_boc("SM-ABC123")) == "SM-ABC123"
    assert parse_plain_comment(comment_boc("encrypted", opcode=0x2167DA4B)) is None
    assert parse_plain_comment("not-base64") is None


def test_scanner_advances_past_finalized_non_payment_transactions():
    client = object.__new__(TonCenterClient)

    async def get(path, params):
        return {"transactions": [{
            "hash": "wallet-command", "lt": "200", "finality": "finalized",
            "in_msg": {"destination": V4_ADDRESS, "value": None},
        }]}

    client._get = get
    batch = asyncio.run(client.scan_gram(V4_ADDRESS, int(NOW.timestamp()), None))
    assert batch.candidates == ()
    assert (batch.logical_time, batch.tx_hash) == (200, "wallet-command")


def test_gram_conversion_accepts_only_final_inbound_transfer():
    payload = {
        "transactions": [
            {
                "hash": "valid-gram",
                "lt": "101",
                "now": int(NOW.timestamp()),
                "finality": 2,
                "description": {"aborted": False},
                "in_msg": {
                    "destination": V4_ADDRESS,
                    "value": "2000000000",
                    "bounced": False,
                    "message_content": {"body": comment_boc("SM-GRAM")},
                },
            },
            {
                "hash": "pending-gram",
                "lt": "102",
                "now": int(NOW.timestamp()),
                "finality": 1,
                "description": {"aborted": False},
                "in_msg": {
                    "destination": V4_ADDRESS,
                    "value": "2000000000",
                    "bounced": False,
                    "message_content": {"body": comment_boc("SM-PENDING")},
                },
            },
        ]
    }

    candidates = parse_gram_transactions(payload, V4_ADDRESS)

    assert [candidate.tx_hash for candidate in candidates] == ["valid-gram"]
    assert candidates[0].units == 2_000_000_000
    assert candidates[0].comment == "SM-GRAM"


@pytest.mark.parametrize("value", [None, "0"])
def test_gram_conversion_ignores_external_messages_without_value(value):
    payload = {
        "transactions": [
            {
                "hash": "external-wallet-command",
                "lt": "100",
                "now": int(NOW.timestamp()),
                "finality": "finalized",
                "description": {"aborted": False},
                "in_msg": {
                    "destination": V4_ADDRESS,
                    "value": value,
                    "bounced": False,
                    "message_content": {"body": comment_boc("not-a-payment")},
                },
            }
        ]
    }

    assert parse_gram_transactions(payload, V4_ADDRESS) == []


def test_gram_conversion_accepts_current_finality_and_fully_credited_uninit_wallet():
    payload = {
        "transactions": [
            {
                "hash": "first-wallet-credit",
                "lt": "103",
                "now": int(NOW.timestamp()),
                "finality": "finalized",
                "description": {
                    "aborted": True,
                    "credit_ph": {"credit": "618750000"},
                },
                "in_msg": {
                    "destination": V5_ADDRESS,
                    "value": "618750000",
                    "bounced": False,
                    "message_content": {"body": comment_boc("SM-FIRST")},
                },
            },
            {
                "hash": "not-final",
                "lt": "104",
                "now": int(NOW.timestamp()),
                "finality": "unfinalized",
                "description": {"aborted": False},
                "in_msg": {
                    "destination": V5_ADDRESS,
                    "value": "618750000",
                    "bounced": False,
                    "message_content": {"body": comment_boc("SM-FIRST")},
                },
            },
        ]
    }

    candidates = parse_gram_transactions(payload, V5_ADDRESS)

    assert [candidate.tx_hash for candidate in candidates] == ["first-wallet-credit"]
    assert candidates[0].aborted is False


def test_usdt_conversion_rejects_fake_master_and_preserves_base_units():
    transfer = {
        "transaction_hash": "valid-usdt",
        "transaction_lt": "201",
        "transaction_now": int(NOW.timestamp()),
        "transaction_aborted": False,
        "destination": V4_ADDRESS,
        "amount": "3500000",
        "forward_payload": comment_boc("SM-USDT"),
        "jetton_master": CANONICAL_USDT_MASTER,
    }
    fake = transfer | {
        "transaction_hash": "fake-usdt",
        "transaction_lt": "202",
        "jetton_master": V5_ADDRESS,
    }

    candidates = parse_usdt_transfers(
        {"jetton_transfers": [transfer, fake]}, V4_ADDRESS
    )

    assert [candidate.tx_hash for candidate in candidates] == ["valid-usdt"]
    assert candidates[0].asset is Asset.USDT
    assert candidates[0].units == 3_500_000
    assert candidates[0].jetton_master == CANONICAL_USDT_MASTER


class FakeTonClient:
    def __init__(self, gram):
        self.gram = gram

    async def scan_gram(self, owner, start_utime, cursor):
        return ScanBatch(tuple(self.gram), 101, "valid-gram")

    async def scan_usdt(self, owner, start_utime, cursor):
        return ScanBatch((), None, None)


def test_scanner_matches_reference_through_repository_and_advances_cursor(tmp_path):
    async def scenario():
        repo = await Repository.open(tmp_path / "bot.sqlite3")
        await repo.setup()
        try:
            order = await repo.create_order(
                user_id=1001,
                product=Product.STARS,
                recipient="recipient",
                product_amount=100,
                months=None,
                asset=Asset.GRAM,
                quoted_api_units=1_000_000_000,
                idempotency_key="order-1",
                created_at=NOW,
            )
            invoice = Invoice(
                destination=V4_ADDRESS,
                asset=Asset.GRAM,
                units=2_000_000_000,
                reference="SM-GRAM",
                created_at=NOW,
                expires_at=NOW + timedelta(minutes=15),
            )
            assert await repo.set_invoice(order.id, invoice)
            candidate = parse_gram_transactions(
                {
                    "transactions": [
                        {
                            "hash": "valid-gram",
                            "lt": "101",
                            "now": int((NOW + timedelta(minutes=1)).timestamp()),
                            "finality": 2,
                            "description": {"aborted": False},
                            "in_msg": {
                                "destination": V4_ADDRESS,
                                "value": "2000000000",
                                "bounced": False,
                                "message_content": {"body": comment_boc("SM-GRAM")},
                            },
                        }
                    ]
                },
                V4_ADDRESS,
            )[0]
            scanner = PaymentScanner(repo, FakeTonClient([candidate]), V4_ADDRESS)

            result = await scanner.scan_once(now=NOW + timedelta(minutes=2))

            saved = await repo.get_order(order.id)
            cursor = await repo.get_scanner_cursor("gram")
            assert result.matched == 1
            assert saved.state is OrderState.PAID
            assert saved.payment_hash == "valid-gram"
            assert (cursor.logical_time, cursor.tx_hash) == (101, "valid-gram")
            replay = await scanner.scan_once(now=NOW + timedelta(minutes=3))
            assert replay.matched == replay.unmatched == 0
        finally:
            await repo.close()

    asyncio.run(scenario())


def test_expired_invoice_payment_is_still_detected_without_starting_purchase(tmp_path):
    async def scenario():
        repo = await Repository.open(tmp_path / "bot.sqlite3")
        await repo.setup()
        try:
            order = await repo.create_order(
                user_id=1001, product=Product.STARS, recipient="recipient",
                product_amount=50, months=None, asset=Asset.GRAM,
                quoted_api_units=1_000_000_000, idempotency_key="late-order", created_at=NOW,
            )
            invoice = Invoice(V4_ADDRESS, Asset.GRAM, 1_100_000_000, "SM-LATE", NOW, NOW + timedelta(minutes=15))
            assert await repo.set_invoice(order.id, invoice)
            assert await repo.expire_order(order.id, now=NOW + timedelta(minutes=16))
            from stars_market_bot.domain import PaymentCandidate
            payment = PaymentCandidate(
                tx_hash="late-payment", logical_time=101, destination=V4_ADDRESS,
                asset=Asset.GRAM, units=invoice.units, comment=invoice.reference,
                timestamp=NOW + timedelta(minutes=16), finalized=True, aborted=False, bounced=False,
            )
            scanner = PaymentScanner(repo, FakeTonClient([payment]), V4_ADDRESS)
            result = await scanner.scan_once(now=NOW + timedelta(minutes=17))
            assert result.unmatched == 1
            assert result.matched == 0
            assert await repo.has_payment(payment.tx_hash)
            assert (await repo.get_order(order.id)).state is OrderState.EXPIRED
            assert await repo.claim_paid_order() is None
        finally:
            await repo.close()

    asyncio.run(scenario())
