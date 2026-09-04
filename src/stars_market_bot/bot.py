import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html import escape
import logging
import secrets
from urllib.parse import quote

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .db import OrderRecord, Repository
from .domain import (
    ASSET_DECIMALS,
    Asset,
    Invoice,
    OrderState,
    Product,
    build_payment_link,
    normalize_username,
    quote_customer_amount,
)
from .fragment import FragmentClient, FragmentPermanentError, FragmentTemporaryError
from .texts import Language, text
from .ui import CUSTOM_EMOJI, CopyControl, Metric, Screen, show_screen


log = logging.getLogger(__name__)


class PurchaseFlow(StatesGroup):
    amount = State()
    recipient = State()
    asset = State()
    confirm = State()


async def payment_asset_ready(
    asset: Asset,
    check_usdt_ready: Callable[[], Awaitable[bool]] | None,
) -> bool:
    if asset is not Asset.USDT or check_usdt_ready is None:
        return True
    try:
        return await check_usdt_ready()
    except Exception:
        return False


async def fetch_checkout(
    fragment: FragmentClient,
    product: Product,
    recipient: str,
    amount: int,
    asset: Asset,
):
    availability, quote = await asyncio.gather(
        fragment.check(product, recipient, amount, asset),
        fragment.quote(product, amount),
    )
    return availability, quote


def _button(
    label: str,
    data: str,
    icon: str | None = None,
    style: str | None = None,
) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=label,
        callback_data=data,
        icon_custom_emoji_id=CUSTOM_EMOJI[icon][1] if icon else None,
        style=style,
    )


def language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_button("Русский", "lang:ru"), _button("English", "lang:en")],
        [_button("Українська", "lang:uk"), _button("Türkçe", "lang:tr")],
    ])


def main_menu_keyboard(language: Language) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_button(text(language, "buy_stars"), "menu:stars", "gift", "primary")],
        [_button(text(language, "buy_premium"), "menu:premium", "premium", "primary")],
        [_button(text(language, "my_orders"), "menu:orders", "orders")],
        [_button(text(language, "api"), "menu:api", "api"),
         _button(text(language, "language"), "menu:lang", "language")],
    ])


def amount_keyboard(language: Language) -> InlineKeyboardMarkup:
    rows = [[_button(str(value), f"amount:{value}") for value in (50, 100, 250)],
            [_button(str(value), f"amount:{value}") for value in (500, 1000)],
            [_button(text(language, "custom_amount"), "amount:custom")],
            [_button(text(language, "cancel"), "cancel")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def duration_keyboard(language: Language) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_button("3", "duration:3"), _button("6", "duration:6"), _button("12", "duration:12")],
        [_button(text(language, "cancel"), "cancel")],
    ])


def recipient_keyboard(language: Language, has_username: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_username:
        rows.append([_button(text(language, "for_me"), "recipient:self")])
    rows.extend([
        [_button(text(language, "enter_username"), "recipient:manual")],
        [_button(text(language, "cancel"), "cancel")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def asset_keyboard(language: Language) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_button("GRAM", "asset:gram"), _button("USDT", "asset:usdt")],
        [_button(text(language, "cancel"), "cancel")],
    ])


def all_callback_samples() -> tuple[str, ...]:
    return (
        "lang:ru", "lang:en", "lang:uk", "lang:tr", "menu:stars",
        "menu:premium", "menu:orders", "menu:api", "menu:lang",
        "amount:1000", "amount:custom", "duration:12", "recipient:self",
        "recipient:manual", "asset:gram", "asset:usdt", "confirm", "cancel",
        "check:2147483647",
    )


def _display_units(units: int, asset: Asset) -> str:
    decimals = ASSET_DECIMALS[asset]
    whole, fraction = divmod(units, 10 ** decimals)
    fractional_text = f"{fraction:0{decimals}d}".rstrip("0")
    return f"{whole}.{fractional_text}" if fractional_text else str(whole)


def home_screen(language: Language) -> Screen:
    return Screen(
        title=text(language, "welcome"),
        lead=text(language, "welcome_lead"),
        emoji="hello",
        banner="home.png",
    )


def product_screen(language: Language, product: Product) -> Screen:
    is_stars = product is Product.STARS
    return Screen(
        title=text(language, "buy_stars" if is_stars else "buy_premium"),
        lead=text(language, "choose_amount" if is_stars else "choose_duration"),
        emoji="gift" if is_stars else "premium",
        banner="stars.png" if is_stars else "premium.png",
    )


def confirmation_screen(
    language: Language,
    *,
    product: str,
    recipient: str,
    price: str,
) -> Screen:
    return Screen(
        title=text(language, "confirm_title"),
        lead=text(language, "confirm_lead"),
        emoji="confirm",
        metrics=(
            Metric(text(language, "product_label"), product),
            Metric(text(language, "recipient_label"), recipient),
            Metric(text(language, "payment_label"), price),
        ),
    )


def invoice_screen(
    language: Language,
    *,
    order_id: int,
    price: str,
    invoice: Invoice,
) -> Screen:
    return Screen(
        title=text(language, "invoice_title", order_id=order_id),
        lead=text(language, "invoice_lead"),
        emoji="payment",
        metrics=(
            Metric(text(language, "payment_label"), f"{price} {invoice.asset.value.upper()}"),
            Metric(text(language, "expires_label"), invoice.expires_at.strftime("%H:%M UTC")),
        ),
        details=(
            text(language, "invoice_title", order_id=order_id),
            text(
                language,
                "invoice_details",
                address=invoice.destination,
                reference=invoice.reference,
            ),
        ),
        copy_controls=(
            CopyControl(text(language, "copy_address"), invoice.destination),
            CopyControl(text(language, "copy_reference"), invoice.reference),
            CopyControl(text(language, "copy_order"), str(order_id)),
        ),
    )


async def _language(repo: Repository, user_id: int) -> Language:
    value = await repo.get_language(user_id)
    return Language(value or Language.RU)


async def _screen(
    event: Message | CallbackQuery,
    screen: Screen,
    keyboard: InlineKeyboardMarkup,
) -> None:
    await show_screen(event, screen, keyboard)


def _product_text(language: Language, product: Product, amount: int) -> str:
    return text(language, product.value, amount=amount)


def _order_status(language: Language, order: OrderRecord) -> str:
    if order.state is OrderState.COMPLETED:
        return text(language, "completed", order_id=order.id,
                    tx_hash=escape(order.final_transaction_hash or "—"))
    if order.state is OrderState.EXPIRED:
        return text(language, "expired", order_id=order.id)
    if order.state is OrderState.MANUAL_REVIEW:
        return text(language, "manual_review", order_id=order.id)
    if order.state is OrderState.FAILED:
        return text(language, "failed", order_id=order.id)
    if order.state in {OrderState.PAID, OrderState.PURCHASING}:
        return text(language, "payment_found")
    if order.state is OrderState.RECONCILIATION_REQUIRED:
        return text(language, "manual_review", order_id=order.id)
    return text(language, "payment_waiting")


def order_screen(language: Language, order: OrderRecord) -> Screen:
    completed = order.state is OrderState.COMPLETED
    amount = order.product_amount or order.months or 0
    return Screen(
        title=(text(language, "completed_title", order_id=order.id) if completed
               else f'{text(language, "order_label")} #{order.id}'),
        lead=(text(language, "completed_lead") if completed
              else _order_status(language, order)),
        emoji="success" if completed else "orders",
        metrics=(
            Metric(text(language, "product_label"), _product_text(language, order.product, amount)),
            Metric(text(language, "recipient_label"), f"@{order.recipient}"),
            Metric(text(language, "payment_label"),
                   f"{_display_units(order.customer_units or 0, order.asset)} {order.asset.value.upper()}"),
        ),
        items=(text(language, "api_promo"),) if completed else (),
        details=(text(language, "transaction_label"),
                 f"{order.final_transaction_hash or '—'}\n{order.fragment_purchase_id or '—'}") if completed else None,
        copy_controls=(
            CopyControl(text(language, "copy_order"), str(order.id)),
            *((CopyControl(text(language, "copy_transaction"), order.final_transaction_hash),)
              if order.final_transaction_hash else ()),
            *((CopyControl(text(language, "copy_purchase"), order.fragment_purchase_id),)
              if order.fragment_purchase_id else ()),
        ),
    )


def order_keyboard(language: Language, order: OrderRecord) -> InlineKeyboardMarkup:
    rows = []
    if order.state is OrderState.AWAITING_PAYMENT and order.invoice:
        rows.append([InlineKeyboardButton(text=text(language, "open_wallet"),
                                          url=build_payment_link(order.invoice))])
        rows.append([_button(text(language, "check_payment"), f"check:{order.id}", "confirm", "primary")])
    if order.final_transaction_hash:
        rows.append([InlineKeyboardButton(
            text=text(language, "transaction_label"),
            url="https://tonviewer.com/transaction/" + quote(order.final_transaction_hash, safe=""),
        )])
    rows.append([_button(text(language, "my_orders"), "menu:orders", "orders")])
    rows.append([_button(text(language, "back"), "cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_router(
    repo: Repository,
    fragment: FragmentClient,
    settings,
    *,
    request_scan: Callable[[], Awaitable[object]] | None = None,
    check_usdt_ready: Callable[[], Awaitable[bool]] | None = None,
) -> Router:
    router = Router(name="store")

    @router.message(Command("retry"))
    async def retry_order(message: Message) -> None:
        if message.from_user.id != settings.owner_telegram_id:
            return
        parts = (message.text or "").split()
        try:
            order_id = int(parts[1])
        except (IndexError, ValueError):
            await message.answer("Usage: /retry ORDER_ID")
            return
        retried = await repo.retry_paid_order(
            order_id,
            "order-retry-" + secrets.token_urlsafe(18),
        )
        await message.answer(
            f"Order #{order_id} queued for retry."
            if retried
            else f"Order #{order_id} cannot be retried."
        )

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        saved = await repo.get_language(message.from_user.id)
        if saved is None:
            await _screen(
                message,
                Screen(
                    title=text(Language.EN, "choose_language"),
                    lead=text(Language.EN, "welcome"),
                    emoji="language",
                ),
                language_keyboard(),
            )
        else:
            language = Language(saved)
            await _screen(message, home_screen(language), main_menu_keyboard(language))

    @router.callback_query(F.data.startswith("lang:"))
    async def select_language(callback: CallbackQuery, state: FSMContext) -> None:
        try:
            language = Language(callback.data.split(":", 1)[1])
        except (ValueError, IndexError):
            await callback.answer()
            return
        await repo.set_language(callback.from_user.id, language.value)
        await state.clear()
        await _screen(callback, home_screen(language), main_menu_keyboard(language))

    @router.callback_query(F.data == "menu:lang")
    async def choose_language(callback: CallbackQuery) -> None:
        await _screen(
            callback,
            Screen(
                title=text(Language.EN, "choose_language"),
                lead=text(Language.EN, "welcome"),
                emoji="language",
            ),
            language_keyboard(),
        )

    @router.callback_query(F.data == "menu:stars")
    async def stars(callback: CallbackQuery, state: FSMContext) -> None:
        language = await _language(repo, callback.from_user.id)
        await state.clear()
        await state.update_data(product=Product.STARS.value)
        await state.set_state(PurchaseFlow.amount)
        await _screen(
            callback,
            product_screen(language, Product.STARS),
            amount_keyboard(language),
        )

    @router.callback_query(F.data == "menu:premium")
    async def premium(callback: CallbackQuery, state: FSMContext) -> None:
        language = await _language(repo, callback.from_user.id)
        await state.clear()
        await state.update_data(product=Product.PREMIUM.value)
        await state.set_state(PurchaseFlow.amount)
        await _screen(
            callback,
            product_screen(language, Product.PREMIUM),
            duration_keyboard(language),
        )

    async def show_recipient(event: Message | CallbackQuery, state: FSMContext, amount: int) -> None:
        language = await _language(repo, event.from_user.id)
        data = await state.get_data()
        product = Product(data["product"])
        await state.update_data(amount=amount)
        await state.set_state(PurchaseFlow.recipient)
        await _screen(
            event,
            Screen(
                title=text(language, "choose_recipient"),
                lead=_product_text(language, product, amount),
                emoji="person",
                metrics=(
                    Metric(
                        text(language, "product_label"),
                        _product_text(language, product, amount),
                    ),
                ),
            ),
            recipient_keyboard(language, bool(event.from_user.username)),
        )

    @router.callback_query(PurchaseFlow.amount, F.data.startswith("amount:"))
    async def preset_amount(callback: CallbackQuery, state: FSMContext) -> None:
        value = callback.data.split(":", 1)[1]
        if value == "custom":
            language = await _language(repo, callback.from_user.id)
            await callback.answer()
            await callback.message.answer(text(language, "choose_amount"))
            return
        await show_recipient(callback, state, int(value))

    @router.callback_query(PurchaseFlow.amount, F.data.startswith("duration:"))
    async def duration(callback: CallbackQuery, state: FSMContext) -> None:
        await show_recipient(callback, state, int(callback.data.split(":", 1)[1]))

    @router.message(PurchaseFlow.amount)
    async def custom_amount(message: Message, state: FSMContext) -> None:
        language = await _language(repo, message.from_user.id)
        data = await state.get_data()
        if data.get("product") != Product.STARS.value:
            return
        try:
            amount = int(message.text or "")
            if not 50 <= amount <= 1_000_000:
                raise ValueError
        except ValueError:
            await message.answer(text(language, "invalid_amount"))
            return
        await show_recipient(message, state, amount)

    @router.callback_query(PurchaseFlow.recipient, F.data == "recipient:manual")
    async def manual_recipient(callback: CallbackQuery) -> None:
        language = await _language(repo, callback.from_user.id)
        await callback.answer()
        await callback.message.answer(text(language, "enter_username"))

    async def accept_recipient(event: Message | CallbackQuery, state: FSMContext, value: str) -> None:
        language = await _language(repo, event.from_user.id)
        try:
            username = normalize_username(value)
        except (TypeError, ValueError):
            if isinstance(event, CallbackQuery):
                await event.answer(text(language, "invalid_username"), show_alert=True)
            else:
                await event.answer(text(language, "invalid_username"))
            return
        await state.update_data(recipient=username)
        await state.set_state(PurchaseFlow.asset)
        await _screen(
            event,
            Screen(
                title=text(language, "choose_asset"),
                lead=text(language, "recipient_ok", username=f"@{username}"),
                emoji="payment",
                metrics=(
                    Metric(text(language, "recipient_label"), f"@{username}"),
                ),
            ),
            asset_keyboard(language),
        )

    @router.callback_query(PurchaseFlow.recipient, F.data == "recipient:self")
    async def self_recipient(callback: CallbackQuery, state: FSMContext) -> None:
        await accept_recipient(callback, state, callback.from_user.username or "")

    @router.message(PurchaseFlow.recipient)
    async def recipient_message(message: Message, state: FSMContext) -> None:
        await accept_recipient(message, state, message.text or "")

    @router.callback_query(PurchaseFlow.asset, F.data.startswith("asset:"))
    async def choose_asset(callback: CallbackQuery, state: FSMContext) -> None:
        language = await _language(repo, callback.from_user.id)
        data = await state.get_data()
        try:
            asset = Asset(callback.data.split(":", 1)[1])
            if not await payment_asset_ready(asset, check_usdt_ready):
                await _screen(
                    callback,
                    Screen(
                        title=text(language, "usdt_unavailable_title"),
                        lead=text(language, "usdt_unavailable"),
                        emoji="error",
                    ),
                    asset_keyboard(language),
                )
                return
            product = Product(data["product"])
            amount = int(data["amount"])
            recipient = str(data["recipient"])
            check, quote = await fetch_checkout(
                fragment,
                product,
                recipient,
                amount,
                asset,
            )
            if not check.available:
                await _screen(
                    callback,
                    Screen(
                        title=text(language, "service_error"),
                        lead=text(language, "unavailable", message=check.message),
                        emoji="error",
                    ),
                    main_menu_keyboard(language),
                )
                await state.clear()
                return
            api_amount = quote.prices.gram if asset is Asset.GRAM else quote.prices.usdt
            api_money = quote_customer_amount(api_amount, Decimal("0"), asset)
            customer = quote_customer_amount(api_amount, settings.commission_percent, asset)
        except (FragmentTemporaryError, FragmentPermanentError, KeyError, ValueError) as error:
            log.warning(
                "checkout_preflight_failed user_id=%s error=%s code=%s",
                callback.from_user.id,
                type(error).__name__,
                getattr(error, "code", None),
            )
            await _screen(
                callback,
                Screen(
                    title=text(language, "service_error"),
                    lead=text(language, "service_error"),
                    emoji="error",
                ),
                main_menu_keyboard(language),
            )
            await state.clear()
            return
        await state.update_data(asset=asset.value, quoted_api_units=api_money.units,
                                customer_units=customer.units)
        await state.set_state(PurchaseFlow.confirm)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [_button(text(language, "confirm_button"), "confirm", "confirm", "primary")],
            [_button(text(language, "cancel"), "cancel", "error", "danger")],
        ])
        await _screen(
            callback,
            confirmation_screen(
                language,
                product=_product_text(language, product, amount),
                recipient=f"@{recipient}",
                price=f"{_display_units(customer.units, asset)} {asset.value.upper()}",
            ),
            keyboard,
        )

    @router.callback_query(PurchaseFlow.confirm, F.data == "confirm")
    async def confirm(callback: CallbackQuery, state: FSMContext) -> None:
        language = await _language(repo, callback.from_user.id)
        data = await state.get_data()
        try:
            product = Product(data["product"])
            asset = Asset(data["asset"])
            amount = int(data["amount"])
            now = datetime.now(timezone.utc)
            order = await repo.create_order(
                user_id=callback.from_user.id, product=product,
                recipient=str(data["recipient"]),
                product_amount=amount if product is Product.STARS else None,
                months=amount if product is Product.PREMIUM else None,
                asset=asset, quoted_api_units=int(data["quoted_api_units"]),
                idempotency_key="order-" + secrets.token_urlsafe(18), created_at=now,
            )
            invoice = Invoice(
                destination=settings.owner_wallet_address, asset=asset,
                units=int(data["customer_units"]), reference="SM-" + secrets.token_hex(5).upper(),
                created_at=now, expires_at=now + timedelta(seconds=settings.invoice_ttl_seconds),
            )
            if not await repo.set_invoice(order.id, invoice):
                raise RuntimeError("invoice state changed")
        except Exception:
            log.exception("invoice_creation_failed user_id=%s", callback.from_user.id)
            await _screen(
                callback,
                Screen(
                    title=text(language, "service_error"),
                    lead=text(language, "service_error"),
                    emoji="error",
                ),
                main_menu_keyboard(language),
            )
            await state.clear()
            return
        await state.clear()
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=text(language, "open_wallet"), url=build_payment_link(invoice))],
            [_button(text(language, "check_payment"), f"check:{order.id}", "confirm", "primary")],
            [_button(text(language, "back"), "cancel")],
        ])
        await _screen(
            callback,
            invoice_screen(
                language,
                order_id=order.id,
                price=_display_units(invoice.units, asset),
                invoice=invoice,
            ),
            keyboard,
        )

    @router.callback_query(F.data.startswith("check:"))
    async def check_payment(callback: CallbackQuery) -> None:
        language = await _language(repo, callback.from_user.id)
        try:
            order_id = int(callback.data.split(":", 1)[1])
            order = await repo.get_order(order_id, user_id=callback.from_user.id)
            if order is None:
                raise ValueError
            if request_scan and order.state is OrderState.AWAITING_PAYMENT:
                await request_scan()
                order = await repo.get_order(order_id, user_id=callback.from_user.id)
        except Exception:
            log.exception("payment_check_failed user_id=%s", callback.from_user.id)
            await callback.answer(text(language, "service_error"), show_alert=True)
            return
        await callback.answer(_order_status(language, order), show_alert=True)

    @router.callback_query(F.data == "menu:orders")
    async def orders(callback: CallbackQuery) -> None:
        language = await _language(repo, callback.from_user.id)
        values = await repo.list_user_orders(callback.from_user.id, limit=10)
        items = tuple(
            text(
                language,
                "order_line",
                order_id=item.id,
                product=_product_text(
                    language,
                    item.product,
                    item.product_amount or item.months or 0,
                ),
                state=text(language, f"state_{item.state.value}"),
            )
            for item in values
        )
        await _screen(
            callback,
            Screen(
                title=text(language, "my_orders"),
                lead=text(language, "no_orders") if not items else text(language, "my_orders"),
                emoji="orders",
                items=items,
            ),
            InlineKeyboardMarkup(inline_keyboard=[
                [_button(f'#{item.id} · {text(language, f"state_{item.state.value}")}', f"order:{item.id}")]
                for item in values
            ] + [[_button(text(language, "back"), "cancel")]]),
        )

    @router.callback_query(F.data.startswith("order:"))
    async def open_order(callback: CallbackQuery) -> None:
        language = await _language(repo, callback.from_user.id)
        try:
            order_id = int(callback.data.split(":", 1)[1])
            order = await repo.get_order(order_id, user_id=callback.from_user.id)
        except ValueError:
            order = None
        if order is None:
            await callback.answer(text(language, "no_orders"), show_alert=True)
            return
        await repo.expire_order(order.id, now=datetime.now(timezone.utc))
        order = await repo.get_order(order.id, user_id=callback.from_user.id)
        screen = (invoice_screen(language, order_id=order.id,
                                 price=_display_units(order.customer_units, order.asset),
                                 invoice=order.invoice)
                  if order.state is OrderState.AWAITING_PAYMENT else order_screen(language, order))
        await _screen(callback, screen, order_keyboard(language, order))

    @router.callback_query(F.data == "menu:api")
    async def api_promo(callback: CallbackQuery) -> None:
        language = await _language(repo, callback.from_user.id)
        await _screen(
            callback,
            Screen(
                title=text(language, "api"),
                lead=text(language, "api_promo"),
                emoji="api",
            ),
            main_menu_keyboard(language),
        )

    @router.callback_query(F.data == "cancel")
    async def cancel(callback: CallbackQuery, state: FSMContext) -> None:
        language = await _language(repo, callback.from_user.id)
        await state.clear()
        await _screen(callback, home_screen(language), main_menu_keyboard(language))

    @router.callback_query()
    async def recover_stale_callback(callback: CallbackQuery, state: FSMContext) -> None:
        language = await _language(repo, callback.from_user.id)
        log.warning(
            "stale_callback_recovered user_id=%s callback=%s",
            callback.from_user.id,
            (callback.data or "").split(":", 1)[0],
        )
        await state.clear()
        await _screen(
            callback,
            Screen(
                title=text(language, "welcome"),
                lead=text(language, "stale_action"),
                emoji="hello",
                banner="home.png",
            ),
            main_menu_keyboard(language),
        )

    @router.message()
    async def recover_unexpected_message(message: Message, state: FSMContext) -> None:
        saved = await repo.get_language(message.from_user.id)
        log.warning("unexpected_message_recovered user_id=%s", message.from_user.id)
        await state.clear()
        if saved is None:
            await _screen(
                message,
                Screen(
                    title=text(Language.EN, "choose_language"),
                    lead=text(Language.EN, "welcome"),
                    emoji="language",
                ),
                language_keyboard(),
            )
            return
        language = Language(saved)
        await _screen(
            message,
            Screen(
                title=text(language, "welcome"),
                lead=text(language, "stale_action"),
                emoji="hello",
                banner="home.png",
            ),
            main_menu_keyboard(language),
        )

    return router
