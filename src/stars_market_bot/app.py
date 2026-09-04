from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import logging
import os
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import BotCommand, FSInputFile, Update
from fragment_api import FragmentAPI

from .bot import build_router, order_keyboard, order_screen
from .config import Settings, load_env_file
from .db import OrderRecord, Repository
from .domain import Asset, OrderState, Product, quote_customer_amount
from .fragment import FragmentClient, FragmentPermanentError, FragmentTemporaryError
from .texts import Language, text
from .ton import PaymentScanner, TonCenterClient, TonCenterTemporaryError, validate_owner_wallet
from .ui import Screen, send_screen


log = logging.getLogger("stars_market_bot")
MIN_USDT_GAS_BALANCE = 1_000_000_000


async def configure_bot_commands(bot: Bot) -> None:
    for language_code, description in (
        (None, "Open the store"),
        ("ru", "Открыть магазин"),
        ("en", "Open the store"),
        ("uk", "Відкрити магазин"),
        ("tr", "Mağazayı aç"),
    ):
        try:
            await bot.set_my_commands(
                [BotCommand(command="start", description=description)],
                language_code=language_code,
            )
        except Exception as error:
            log.warning("bot_commands_setup_failed language=%s error=%s",
                        language_code, type(error).__name__)


@dataclass
class AppContext:
    settings: Any
    repo: Repository
    fragment: Any
    scanner: Any
    bot: Any


def _amount(order: OrderRecord) -> int:
    return order.product_amount if order.product is Product.STARS else order.months  # type: ignore[return-value]


async def _safe_notify(context: AppContext, chat_id: int, body: str) -> None:
    try:
        await context.bot.send_message(chat_id, body)
    except Exception:
        log.warning("notification_failed chat=%s", chat_id)


async def _safe_notify_screen(context: AppContext, chat_id: int, screen: Screen, keyboard=None) -> None:
    try:
        await send_screen(context.bot, chat_id, screen, keyboard)
    except Exception:
        log.warning("notification_failed chat=%s", chat_id)


async def _user_language(context: AppContext, user_id: int) -> Language:
    value = await context.repo.get_language(user_id)
    return Language(value or Language.RU)


async def _manual(context: AppContext, order: OrderRecord, code: str) -> None:
    if await context.repo.finish_order(
        order.id, OrderState.MANUAL_REVIEW, error_code=code,
        error_message="Manual review required",
    ):
        language = await _user_language(context, order.user_id)
        await _safe_notify(
            context,
            order.user_id,
            text(language, "manual_review", order_id=order.id),
        )
        await _safe_notify(context, context.settings.owner_telegram_id,
                           f"Manual review: order #{order.id}, code {code}.")


async def _process_purchase(context: AppContext, order: OrderRecord) -> None:
    amount = _amount(order)
    if order.fragment_purchase_id is None:
        try:
            availability = await context.fragment.check(
                order.product, order.recipient, amount, order.asset
            )
            if not availability.available:
                await _manual(context, order, "RECIPIENT_UNAVAILABLE")
                return
            quote = await context.fragment.quote(order.product, amount)
            raw_price = quote.prices.gram if order.asset is Asset.GRAM else quote.prices.usdt
            required = quote_customer_amount(raw_price, Decimal("0"), order.asset)
            if order.customer_units is None or order.customer_units < required.units:
                await _manual(context, order, "PRICE_INCREASE")
                return
            purchase = await context.fragment.create(
                order, context.settings.owner_seed, context.settings.owner_wallet_address
            )
        except FragmentTemporaryError:
            return
        except FragmentPermanentError as error:
            await context.repo.finish_order(
                order.id, OrderState.FAILED, error_code=error.code or "FRAGMENT_REJECTED",
                error_message=error.public_message,
            )
            language = await _user_language(context, order.user_id)
            await _safe_notify(
                context,
                order.user_id,
                text(language, "failed", order_id=order.id),
            )
            await _safe_notify(context, context.settings.owner_telegram_id,
                               f"Failed order #{order.id}: {error.code or 'FRAGMENT_REJECTED'}.")
            return
        if not await context.repo.record_fragment_purchase(order.id, purchase.purchase_id):
            return
        order = await context.repo.get_order(order.id)
        if order is None:
            return
        result = purchase
        if not result.terminal:
            try:
                result = await context.fragment.status(result.purchase_id)
            except FragmentTemporaryError:
                return
            except FragmentPermanentError:
                changed = await context.repo.finish_order(
                    order.id, OrderState.RECONCILIATION_REQUIRED,
                    error_code="STATUS_UNCERTAIN",
                    error_message="Purchase status requires reconciliation",
                )
                if changed:
                    await _safe_notify(context, context.settings.owner_telegram_id,
                                       f"Reconciliation required: order #{order.id}.")
                return
    else:
        try:
            result = await context.fragment.status(order.fragment_purchase_id)
        except FragmentTemporaryError:
            return
        except FragmentPermanentError:
            changed = await context.repo.finish_order(
                order.id, OrderState.RECONCILIATION_REQUIRED,
                error_code="STATUS_UNCERTAIN", error_message="Purchase status requires reconciliation",
            )
            if changed:
                await _safe_notify(context, context.settings.owner_telegram_id,
                                   f"Reconciliation required: order #{order.id}.")
            return

    if not result.terminal:
        return
    if result.status == "completed":
        if not await context.repo.finish_order(
            order.id, OrderState.COMPLETED,
            final_transaction_hash=result.transaction_hash,
        ):
            return
        language = await _user_language(context, order.user_id)
        order = await context.repo.get_order(order.id)
        await _safe_notify_screen(
            context,
            order.user_id,
            order_screen(language, order),
            order_keyboard(language, order),
        )
    elif result.error_code == "TOP_UP_REQUIRED":
        if not await context.repo.finish_order(
            order.id,
            OrderState.MANUAL_REVIEW,
            error_code="TOP_UP_REQUIRED",
            error_message="Owner wallet requires a GRAM top-up",
        ):
            return
        language = await _user_language(context, order.user_id)
        await _safe_notify(
            context,
            order.user_id,
            text(language, "manual_review", order_id=order.id),
        )
        await _safe_notify(
            context,
            context.settings.owner_telegram_id,
            f"Top up the owner wallet, then send /retry {order.id}.",
        )
    elif result.status == "reconciliation_required":
        if order.state is OrderState.RECONCILIATION_REQUIRED:
            return
        if not await context.repo.finish_order(
            order.id, OrderState.RECONCILIATION_REQUIRED,
            error_code="RECONCILIATION_REQUIRED", error_message="Manual reconciliation required",
        ):
            return
        await _safe_notify(context, context.settings.owner_telegram_id,
                           f"Reconciliation required: order #{order.id}.")
    else:
        if not await context.repo.finish_order(
            order.id, OrderState.FAILED,
            error_code="PURCHASE_FAILED", error_message="Fragment purchase failed",
        ):
            return
        language = await _user_language(context, order.user_id)
        await _safe_notify(
            context,
            order.user_id,
            text(language, "failed", order_id=order.id),
        )


async def run_purchase_cycle(context: AppContext) -> int:
    orders = await context.repo.list_purchase_orders(limit=100)
    claimed = await context.repo.claim_paid_order()
    if claimed is not None:
        orders.append(claimed)
    for order in orders:
        await _process_purchase(context, order)
    return len(orders)


async def run_expiry_cycle(
    context: AppContext, *, now: datetime | None = None
) -> int:
    return await context.repo.expire_due_orders(now=now or datetime.now(timezone.utc))


async def run_payment_cycle(context: AppContext) -> int:
    if context.scanner is None:
        return 0
    result = await context.scanner.scan_once()
    if result.unmatched:
        await _safe_notify(
            context,
            context.settings.owner_telegram_id,
            f"Unmatched TON payments detected: {result.unmatched}. Check the payments table.",
        )
    return result.matched


async def _worker(context: AppContext, function, interval: int) -> None:
    while True:
        delay = interval
        try:
            await function(context)
        except asyncio.CancelledError:
            raise
        except TonCenterTemporaryError as error:
            delay = max(interval, error.retry_after)
            log.warning("payment_provider_retry delay_seconds=%.1f", delay)
        except Exception:
            log.exception("worker_cycle_failed")
        await asyncio.sleep(delay)


def create_app(settings: Settings) -> web.Application:
    app = web.Application(client_max_size=2 * 1024 * 1024)
    app["settings"] = settings

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "ready": "context" in app})

    async def webhook(request: web.Request) -> web.Response:
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != settings.webhook_secret:
            raise web.HTTPUnauthorized()
        context: AppContext = app["context"]
        dispatcher: Dispatcher = app["dispatcher"]
        try:
            update = Update.model_validate(await request.json(), context={"bot": context.bot})
        except Exception:
            raise web.HTTPBadRequest() from None
        await dispatcher.feed_update(context.bot, update)
        return web.Response(text="ok")

    app.router.add_get("/health", health)
    app.router.add_post(settings.webhook_path, webhook)

    async def lifecycle(application: web.Application):
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        repo = await Repository.open(settings.database_path)
        await repo.setup()
        session = ClientSession()
        bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        fragment = FragmentClient(FragmentAPI(base_url=settings.fragment_api_url))
        owner_wallet = validate_owner_wallet(settings.owner_seed, settings.owner_wallet_address)
        ton_client = TonCenterClient(session, settings.toncenter_api_url)
        scanner = PaymentScanner(repo, ton_client, owner_wallet)
        scan_lock = asyncio.Lock()

        async def scan_now():
            async with scan_lock:
                return await scanner.scan_once()

        async def usdt_ready():
            return await ton_client.account_balance(owner_wallet) >= MIN_USDT_GAS_BALANCE

        dispatcher = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
        dispatcher.include_router(build_router(
            repo,
            fragment,
            settings,
            request_scan=scan_now,
            check_usdt_ready=usdt_ready,
        ))
        context = AppContext(settings, repo, fragment, scanner, bot)
        application["context"] = context
        application["dispatcher"] = dispatcher
        await configure_bot_commands(bot)
        await bot.set_webhook(
            settings.public_base_url + settings.webhook_path,
            secret_token=settings.webhook_secret,
            certificate=(
                FSInputFile(settings.webhook_certificate_path)
                if settings.webhook_certificate_path is not None
                else None
            ),
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
        tasks = [
            asyncio.create_task(_worker(context, run_payment_cycle, settings.scan_interval_seconds)),
            asyncio.create_task(_worker(context, run_purchase_cycle, 3)),
            asyncio.create_task(_worker(context, run_expiry_cycle, 10)),
        ]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task
            with suppress(Exception):
                await bot.delete_webhook(drop_pending_updates=False)
            await dispatcher.storage.close()
            await bot.session.close()
            await session.close()
            await repo.close()

    app.cleanup_ctx.append(lifecycle)
    return app


def _settings() -> Settings:
    values: dict[str, str] = {}
    env_path = Path(".env")
    if env_path.exists():
        values.update(load_env_file(env_path))
    values.update(os.environ)
    return Settings.from_env(values)


async def run_polling(settings: Settings) -> None:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    repo = await Repository.open(settings.database_path)
    await repo.setup()
    session = ClientSession()
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    fragment = FragmentClient(FragmentAPI(base_url=settings.fragment_api_url))
    owner_wallet = validate_owner_wallet(settings.owner_seed, settings.owner_wallet_address)
    ton_client = TonCenterClient(session, settings.toncenter_api_url)
    scanner = PaymentScanner(repo, ton_client, owner_wallet)
    scan_lock = asyncio.Lock()

    async def scan_now():
        async with scan_lock:
            return await scanner.scan_once()

    async def usdt_ready():
        return await ton_client.account_balance(owner_wallet) >= MIN_USDT_GAS_BALANCE

    dispatcher = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    dispatcher.include_router(build_router(
        repo,
        fragment,
        settings,
        request_scan=scan_now,
        check_usdt_ready=usdt_ready,
    ))
    context = AppContext(settings, repo, fragment, scanner, bot)
    tasks = [
        asyncio.create_task(_worker(context, run_payment_cycle, settings.scan_interval_seconds)),
        asyncio.create_task(_worker(context, run_purchase_cycle, 3)),
        asyncio.create_task(_worker(context, run_expiry_cycle, 10)),
    ]
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await configure_bot_commands(bot)
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        await dispatcher.storage.close()
        await bot.session.close()
        await session.close()
        await repo.close()


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    settings = _settings()
    if settings.delivery_mode == "polling":
        asyncio.run(run_polling(settings))
    else:
        web.run_app(create_app(settings), host="127.0.0.1", port=8080)


if __name__ == "__main__":
    main()
