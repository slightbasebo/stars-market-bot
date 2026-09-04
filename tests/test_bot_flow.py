import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.enums import ChatType, MessageEntityType
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageText,
    GetMe,
    SendMessage,
    SendRichMessage,
)
from aiogram.types import CallbackQuery, Chat, Message, MessageEntity, Update, User
from fragment_api import AvailabilityCheck, PriceQuote, Prices

from stars_market_bot.bot import build_router
from stars_market_bot.db import Repository
from stars_market_bot.domain import Asset, OrderState, Product


NOW = datetime(2026, 9, 5, tzinfo=timezone.utc)
USER = User(
    id=8670053970,
    is_bot=False,
    first_name="Lil dev",
    username="tondotdev",
)
BOT_USER = User(
    id=8905408325,
    is_bot=True,
    first_name="STARS MARKET EXAMPLE",
    username="apiexample_bot",
)
CHAT = Chat(
    id=USER.id,
    type=ChatType.PRIVATE,
    first_name=USER.first_name,
    username=USER.username,
)


class CaptureSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.methods = []
        self.next_message_id = 100

    async def close(self) -> None:
        return None

    async def stream_content(self, *args, **kwargs):
        if False:
            yield b""

    async def make_request(self, bot, method, timeout=None):
        self.methods.append(method)
        if isinstance(method, GetMe):
            return BOT_USER
        if isinstance(method, (AnswerCallbackQuery, DeleteMessage)):
            return True
        if isinstance(method, (SendRichMessage, SendMessage)):
            self.next_message_id += 1
            return Message(
                message_id=self.next_message_id,
                date=NOW,
                chat=CHAT,
                from_user=BOT_USER,
                text=getattr(method, "text", None),
                reply_markup=method.reply_markup,
            )
        if isinstance(method, EditMessageText):
            return Message(
                message_id=method.message_id,
                date=NOW,
                chat=CHAT,
                from_user=BOT_USER,
                text=getattr(method, "text", None),
                reply_markup=method.reply_markup,
            )
        raise AssertionError(type(method).__name__)


class UnusedFragment:
    async def check(self, *args, **kwargs):
        raise AssertionError("stale callback must not reach Fragment")

    async def quote(self, *args, **kwargs):
        raise AssertionError("stale callback must not reach Fragment")


class AvailableFragment:
    async def check(self, product, username, amount, asset):
        return AvailabilityCheck(
            available=True,
            code="AVAILABLE",
            message="The purchase can be created.",
            checked_at="2026-09-05T00:00:00Z",
            action=None,
        )

    async def quote(self, product, amount):
        return PriceQuote(
            product=product.value,
            amount=amount,
            prices=Prices(gram="0.5267", usdt="0.75"),
            stale=False,
        )

def _settings():
    return SimpleNamespace(
        owner_telegram_id=8670053970,
        owner_wallet_address="UQCsFwdmQK_F5uA_5Zf7FrSjhxedNCrOpgbT8hiaR2eFu-vI",
        commission_percent=Decimal("10"),
        invoice_ttl_seconds=900,
    )


def _callback(update_id: int, data: str) -> Update:
    message = Message(
        message_id=42,
        date=NOW,
        chat=CHAT,
        from_user=BOT_USER,
        text="Previous bot screen",
    )
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=str(update_id),
            from_user=USER,
            chat_instance="flow-test",
            message=message,
            data=data,
        ),
    )


def _message(update_id: int, text: str) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=NOW,
            chat=CHAT,
            from_user=USER,
            text=text,
        ),
    )


def _start_message(update_id: int) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=NOW,
            chat=CHAT,
            from_user=USER,
            text="/start",
            entities=[
                MessageEntity(
                    type=MessageEntityType.BOT_COMMAND,
                    offset=0,
                    length=6,
                )
            ],
        ),
    )


def test_stale_state_button_is_acknowledged_and_opens_fresh_menu(tmp_path: Path):
    async def scenario():
        repo = await Repository.open(tmp_path / "bot.sqlite3")
        await repo.setup()
        await repo.set_language(USER.id, "ru")
        session = CaptureSession()
        bot = Bot(
            "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
            session=session,
        )
        dispatcher = Dispatcher(storage=MemoryStorage())
        dispatcher.include_router(build_router(repo, UnusedFragment(), _settings()))
        try:
            await dispatcher.feed_update(bot, _callback(1, "amount:50"))
            return session.methods
        finally:
            await dispatcher.storage.close()
            await bot.session.close()
            await repo.close()

    methods = asyncio.run(scenario())

    assert any(isinstance(method, AnswerCallbackQuery) for method in methods)
    assert any(isinstance(method, SendRichMessage) for method in methods)


def test_text_without_an_active_step_opens_fresh_menu(tmp_path: Path):
    async def scenario():
        repo = await Repository.open(tmp_path / "bot.sqlite3")
        await repo.setup()
        await repo.set_language(USER.id, "ru")
        session = CaptureSession()
        bot = Bot(
            "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
            session=session,
        )
        dispatcher = Dispatcher(storage=MemoryStorage())
        dispatcher.include_router(build_router(repo, UnusedFragment(), _settings()))
        try:
            await dispatcher.feed_update(bot, _message(1, "@recipient"))
            return session.methods
        finally:
            await dispatcher.storage.close()
            await bot.session.close()
            await repo.close()

    methods = asyncio.run(scenario())

    assert any(isinstance(method, SendRichMessage) for method in methods)


def test_stars_and_premium_happy_paths_create_payable_invoices(tmp_path: Path):
    async def scenario():
        repo = await Repository.open(tmp_path / "bot.sqlite3")
        await repo.setup()
        session = CaptureSession()
        bot = Bot(
            "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
            session=session,
        )
        dispatcher = Dispatcher(storage=MemoryStorage())
        dispatcher.include_router(
            build_router(
                repo,
                AvailableFragment(),
                _settings(),
                check_usdt_ready=lambda: asyncio.sleep(0, result=True),
            )
        )
        update_id = 1
        try:
            await dispatcher.feed_update(bot, _start_message(update_id))
            update_id += 1
            for data in (
                "lang:ru",
                "menu:stars",
                "amount:50",
                "recipient:self",
                "asset:gram",
                "confirm",
                "menu:premium",
                "duration:3",
                "recipient:self",
                "asset:usdt",
                "confirm",
            ):
                await dispatcher.feed_update(bot, _callback(update_id, data))
                update_id += 1
            return await repo.list_user_orders(USER.id), session.methods
        finally:
            await dispatcher.storage.close()
            await bot.session.close()
            await repo.close()

    orders, methods = asyncio.run(scenario())

    assert [(order.product, order.asset, order.state) for order in reversed(orders)] == [
        (Product.STARS, Asset.GRAM, OrderState.AWAITING_PAYMENT),
        (Product.PREMIUM, Asset.USDT, OrderState.AWAITING_PAYMENT),
    ]
    assert all(order.recipient == "tondotdev" for order in orders)
    assert any(isinstance(method, SendRichMessage) for method in methods)


def test_custom_stars_amount_and_manual_recipient_reach_invoice(tmp_path: Path):
    async def scenario():
        repo = await Repository.open(tmp_path / "bot.sqlite3")
        await repo.setup()
        session = CaptureSession()
        bot = Bot(
            "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
            session=session,
        )
        dispatcher = Dispatcher(storage=MemoryStorage())
        dispatcher.include_router(
            build_router(repo, AvailableFragment(), _settings())
        )
        update_id = 1
        try:
            for data in ("lang:uk", "menu:stars", "amount:custom"):
                await dispatcher.feed_update(bot, _callback(update_id, data))
                update_id += 1
            await dispatcher.feed_update(bot, _message(update_id, "125"))
            update_id += 1
            await dispatcher.feed_update(
                bot,
                _callback(update_id, "recipient:manual"),
            )
            update_id += 1
            await dispatcher.feed_update(bot, _message(update_id, "@alice_1"))
            update_id += 1
            for data in ("asset:gram", "confirm"):
                await dispatcher.feed_update(bot, _callback(update_id, data))
                update_id += 1
            return (await repo.list_user_orders(USER.id))[0]
        finally:
            await dispatcher.storage.close()
            await bot.session.close()
            await repo.close()

    order = asyncio.run(scenario())

    assert order.product is Product.STARS
    assert order.product_amount == 125
    assert order.recipient == "alice_1"
    assert order.state is OrderState.AWAITING_PAYMENT


@pytest.mark.parametrize("language", ["ru", "en", "uk", "tr"])
@pytest.mark.parametrize("product,selection,asset", [
    ("stars", "amount:50", Asset.GRAM),
    ("premium", "duration:3", Asset.USDT),
])
def test_invoice_payment_delivery_and_receipt_recovery(tmp_path, language, product, selection, asset):
    from datetime import timedelta
    from stars_market_bot.app import AppContext, run_payment_cycle, run_purchase_cycle
    from stars_market_bot.domain import PaymentCandidate, CANONICAL_USDT_MASTER
    from stars_market_bot.fragment import FragmentPurchase
    from stars_market_bot.ton import PaymentScanner, ScanBatch

    class DeliveryFragment(AvailableFragment):
        def __init__(self):
            self.created = []

        async def create(self, order, seed, wallet_address):
            self.created.append(order.idempotency_key)
            return FragmentPurchase("test-purchase", "queued")

        async def status(self, purchase_id):
            return FragmentPurchase(purchase_id, "completed", "a" * 64)

    async def scenario():
        repo = await Repository.open(tmp_path / "bot.sqlite3")
        await repo.setup()
        session = CaptureSession()
        bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi", session=session)
        settings = _settings()
        settings.owner_seed = "test-only-no-real-wallet"
        fragment = DeliveryFragment()
        dispatcher = Dispatcher(storage=MemoryStorage())
        dispatcher.include_router(build_router(repo, fragment, settings))
        try:
            for update_id, data in enumerate((f"lang:{language}", f"menu:{product}",
                                             selection, "recipient:self", f"asset:{asset.value}", "confirm"), 1):
                await dispatcher.feed_update(bot, _callback(update_id, data))
            order = (await repo.list_user_orders(USER.id))[0]
            assert order.state is OrderState.AWAITING_PAYMENT
            await dispatcher.feed_update(bot, _callback(10, "menu:orders"))
            assert any(button.callback_data == f"order:{order.id}"
                       for row in session.methods[-2].reply_markup.inline_keyboard for button in row)
            await dispatcher.feed_update(bot, _callback(11, f"order:{order.id}"))
            invoice_method = next(method for method in reversed(session.methods) if isinstance(method, SendRichMessage))
            assert order.reference in invoice_method.rich_message.html
            assert order.destination in invoice_method.rich_message.html

            payment = PaymentCandidate(
                tx_hash="test-payment", logical_time=1, destination=order.destination,
                asset=asset, units=order.customer_units, comment=order.reference,
                timestamp=order.invoice_created_at + timedelta(seconds=1),
                finalized=True, aborted=False, bounced=False,
                jetton_master=CANONICAL_USDT_MASTER if asset is Asset.USDT else None,
            )

            class Chain:
                async def scan_gram(self, owner, start_utime, cursor):
                    return ScanBatch((payment,), 1, payment.tx_hash)

                scan_usdt = scan_gram

            context = AppContext(settings, repo, fragment,
                                 PaymentScanner(repo, Chain(), settings.owner_wallet_address), bot)
            assert await run_payment_cycle(context) == 1
            await run_purchase_cycle(context)
            await run_purchase_cycle(context)
            assert len(fragment.created) == 1
            assert (await repo.get_order(order.id)).state is OrderState.COMPLETED
            receipt = next(method for method in reversed(session.methods) if isinstance(method, SendRichMessage))
            assert "test-purchase" in receipt.rich_message.html
            assert "a" * 64 in receipt.rich_message.html
            assert any(button.url and "tonviewer.com/transaction/" in button.url
                       for row in receipt.reply_markup.inline_keyboard for button in row)

            await dispatcher.storage.close()
            dispatcher = Dispatcher(storage=MemoryStorage())
            dispatcher.include_router(build_router(repo, fragment, settings))
            await dispatcher.feed_update(bot, _callback(12, f"order:{order.id}"))
            recovered = next(method for method in reversed(session.methods) if isinstance(method, SendRichMessage))
            assert recovered.rich_message.html == receipt.rich_message.html

            foreign = _callback(13, f"order:{order.id}")
            foreign = foreign.model_copy(update={"callback_query": foreign.callback_query.model_copy(
                update={"from_user": USER.model_copy(update={"id": USER.id + 1})}
            )})
            count_before = sum(isinstance(method, SendRichMessage) for method in session.methods)
            await dispatcher.feed_update(bot, foreign)
            assert sum(isinstance(method, SendRichMessage) for method in session.methods) == count_before
        finally:
            await dispatcher.storage.close()
            await bot.session.close()
            await repo.close()

    asyncio.run(scenario())
