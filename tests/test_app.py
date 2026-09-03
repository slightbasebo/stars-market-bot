import asyncio
from datetime import datetime, timedelta, timezone

from aiogram.methods import SendRichMessage

from stars_market_bot.app import (
    AppContext,
    run_expiry_cycle,
    run_payment_cycle,
    run_purchase_cycle,
)
from stars_market_bot.db import Repository
from stars_market_bot.domain import Asset, Invoice, OrderState, Product
from stars_market_bot.fragment import FragmentPurchase, FragmentTemporaryError


NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


class FakeBot:
    def __init__(self):
        self.messages = []
        self.methods = []

    async def __call__(self, method):
        self.methods.append(method)
        return method

    async def send_message(self, *args, **kwargs):
        self.messages.append((args, kwargs))
        return None


class FakeFragment:
    def __init__(self):
        self.create_keys = []
        self.fail_once = True

    async def check(self, product, recipient, amount, asset):
        return type("Check", (), {"available": True})()

    async def quote(self, product, amount):
        prices = type("Prices", (), {"gram": "1", "usdt": "1"})()
        return type("Quote", (), {"prices": prices})()

    async def create(self, order, seed, wallet_address):
        self.create_keys.append(order.idempotency_key)
        if self.fail_once:
            self.fail_once = False
            raise FragmentTemporaryError("temporary")
        return FragmentPurchase("purchase-9", "queued")

    async def status(self, purchase_id):
        return FragmentPurchase(purchase_id, "completed", "chain-hash")


async def prepared_context(tmp_path, *, expires_at=None):
    repo = await Repository.open(tmp_path / "bot.sqlite3")
    await repo.setup()
    order = await repo.create_order(
        user_id=42,
        product=Product.STARS,
        recipient="alice_1",
        product_amount=100,
        months=None,
        asset=Asset.GRAM,
        quoted_api_units=1_000_000_000,
        idempotency_key="order-42",
        created_at=NOW,
    )
    invoice = Invoice(
        destination="UQwallet",
        asset=Asset.GRAM,
        units=1_100_000_000,
        reference="SM-ORDER42",
        created_at=NOW,
        expires_at=expires_at or NOW + timedelta(minutes=15),
    )
    assert await repo.set_invoice(order.id, invoice)
    context = AppContext(
        settings=type("Settings", (), {
            "owner_seed": "seed",
            "owner_wallet_address": "UQwallet",
            "owner_telegram_id": 1,
        })(),
        repo=repo,
        fragment=FakeFragment(),
        scanner=None,
        bot=FakeBot(),
    )
    return context, order.id


def test_paid_order_reuses_idempotency_key_after_temporary_failure(tmp_path):
    async def scenario():
        context, order_id = await prepared_context(tmp_path)
        try:
            candidate = type("Candidate", (), {})
            from stars_market_bot.domain import PaymentCandidate
            payment = PaymentCandidate(
                tx_hash="pay-42", logical_time=1, destination="UQwallet",
                asset=Asset.GRAM, units=1_100_000_000, comment="SM-ORDER42",
                timestamp=NOW + timedelta(minutes=1), finalized=True,
                aborted=False, bounced=False,
            )
            assert await context.repo.record_payment(order_id, payment)
            await run_purchase_cycle(context)
            await run_purchase_cycle(context)
            saved = await context.repo.get_order(order_id)
            assert context.fragment.create_keys == ["order-42", "order-42"]
            assert saved.state is OrderState.COMPLETED
            assert saved.final_transaction_hash == "chain-hash"
            completion = context.bot.methods[-1]
            assert isinstance(completion, SendRichMessage)
            assert "Заказ #" in completion.rich_message.html
            assert "stars-market.duckdns.org/api" in completion.rich_message.html
            assert "github.com/slightbasebo/fragment-api-dev" in completion.rich_message.html
            assert 'type="copy_text" text="chain-hash"' in completion.rich_message.html
        finally:
            await context.repo.close()
    asyncio.run(scenario())


def test_reconciliation_is_polled_silently_until_purchase_completes(tmp_path):
    class ReconciliationFragment:
        def __init__(self):
            self.statuses = [
                FragmentPurchase("purchase-recovery", "reconciliation_required"),
                FragmentPurchase("purchase-recovery", "completed", "settled-hash"),
            ]

        async def status(self, purchase_id):
            assert purchase_id == "purchase-recovery"
            return self.statuses.pop(0)

    async def scenario():
        context, order_id = await prepared_context(tmp_path)
        context.fragment = ReconciliationFragment()
        try:
            from stars_market_bot.domain import PaymentCandidate

            payment = PaymentCandidate(
                tx_hash="pay-recovery",
                logical_time=1,
                destination="UQwallet",
                asset=Asset.GRAM,
                units=1_100_000_000,
                comment="SM-ORDER42",
                timestamp=NOW + timedelta(minutes=1),
                finalized=True,
                aborted=False,
                bounced=False,
            )
            assert await context.repo.record_payment(order_id, payment)
            claimed = await context.repo.claim_paid_order()
            assert await context.repo.record_fragment_purchase(
                claimed.id, "purchase-recovery"
            )
            assert await context.repo.finish_order(
                claimed.id,
                OrderState.RECONCILIATION_REQUIRED,
                error_code="RECONCILIATION_REQUIRED",
                error_message="Manual reconciliation required",
            )

            await run_purchase_cycle(context)
            assert context.bot.messages == []
            assert context.bot.methods == []

            await run_purchase_cycle(context)
            saved = await context.repo.get_order(order_id)
            assert saved.state is OrderState.COMPLETED
            assert saved.final_transaction_hash == "settled-hash"
            assert len(context.bot.methods) == 1
        finally:
            await context.repo.close()

    asyncio.run(scenario())


def test_expiry_cycle_expires_due_invoice(tmp_path):
    async def scenario():
        context, order_id = await prepared_context(tmp_path, expires_at=NOW + timedelta(seconds=1))
        try:
            assert await run_expiry_cycle(context, now=NOW + timedelta(seconds=2)) == 1
            assert (await context.repo.get_order(order_id)).state is OrderState.EXPIRED
        finally:
            await context.repo.close()
    asyncio.run(scenario())


def test_payment_cycle_notifies_owner_about_unmatched_transfer(tmp_path):
    class FakeScanner:
        async def scan_once(self):
            return type("Result", (), {"matched": 0, "unmatched": 2})()

    async def scenario():
        context, _ = await prepared_context(tmp_path)
        context.scanner = FakeScanner()
        try:
            assert await run_payment_cycle(context) == 0
            assert context.bot.messages == [
                (((1, "Unmatched TON payments detected: 2. Check the payments table.")), {})
            ]
        finally:
            await context.repo.close()

    asyncio.run(scenario())
