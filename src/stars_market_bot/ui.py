from __future__ import annotations

import contextlib
from dataclasses import dataclass
from html import escape
import logging
from pathlib import Path

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText, SendMessage, SendRichMessage
from aiogram.types import (
    CallbackQuery,
    CopyTextButton,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputRichMessage,
    InputRichMessageMedia,
    Message,
)


ASSET_DIR = Path(__file__).with_name("assets")
log = logging.getLogger(__name__)

CUSTOM_EMOJI = {
    "api": ("🚀", "5445284980978621387"),
    "confirm": ("☑️", "5454096630372379732"),
    "error": ("❗️", "5467928559664242360"),
    "gift": ("🎁", "5199749070830197566"),
    "hello": ("👋", "5472055112702629499"),
    "language": ("💬", "5465300082628763143"),
    "orders": ("🎫", "5418010521309815154"),
    "payment": ("💎", "5471952986970267163"),
    "person": ("👤", "5373012449597335010"),
    "premium": ("👑", "5467406098367521267"),
    "success": ("✅", "5427009714745517609"),
}


@dataclass(frozen=True, slots=True)
class Metric:
    label: str
    value: str


@dataclass(frozen=True, slots=True)
class CopyControl:
    label: str
    value: str

    def __post_init__(self) -> None:
        if not 1 <= len(self.value) <= 256:
            raise ValueError("copy value must contain 1-256 characters")


@dataclass(frozen=True, slots=True)
class Screen:
    title: str
    lead: str
    emoji: str
    banner: str | None = None
    metrics: tuple[Metric, ...] = ()
    items: tuple[str, ...] = ()
    details: tuple[str, str] | None = None
    copy_controls: tuple[CopyControl, ...] = ()


def _title(screen: Screen) -> str:
    fallback, custom_emoji_id = CUSTOM_EMOJI[screen.emoji]
    return (
        f'<h1><tg-emoji emoji-id="{custom_emoji_id}">{fallback}</tg-emoji> '
        f"{escape(screen.title, quote=True)}</h1>"
    )


def _text(value: str) -> str:
    return escape(value, quote=True).replace("\n", "<br/>")


def build_rich_message(screen: Screen) -> InputRichMessage:
    parts = [_title(screen), f"<p>{_text(screen.lead)}</p>"]
    if screen.metrics:
        rows = "".join(
            "<tr>"
            f"<td>{escape(metric.label, quote=True)}</td>"
            f'<td align="right"><b>{escape(metric.value, quote=True)}</b></td>'
            "</tr>"
            for metric in screen.metrics
        )
        parts.append(
            f"<table bordered striped>{rows}</table>"
        )
    if screen.items:
        parts.append(
            "<ul>"
            + "".join(f"<li>{_text(item)}</li>" for item in screen.items)
            + "</ul>"
        )
    if screen.details:
        summary, detail = screen.details
        parts.append(
            f"<details><summary>{escape(summary, quote=True)}</summary>"
            f"<p>{_text(detail)}</p></details>"
        )
    parts.extend(
        "<tg-button-row>"
        f'<tg-button type="copy_text" text="{escape(control.value, quote=True)}">'
        f"{escape(control.label, quote=True)}</tg-button>"
        "</tg-button-row>"
        for control in screen.copy_controls
    )
    body = "".join(parts)
    if screen.banner is None:
        return InputRichMessage(html=body)
    banner = ASSET_DIR / screen.banner
    if not banner.is_file():
        raise FileNotFoundError(banner)
    return InputRichMessage(
        html='<figure><img src="tg://photo?id=banner"/></figure>' + body,
        media=[
            InputRichMessageMedia(
                id="banner",
                media=InputMediaPhoto(media=FSInputFile(str(banner))),
            )
        ],
    )


def render_compact(screen: Screen) -> str:
    parts = [f"<b>{escape(screen.title, quote=True)}</b>", escape(screen.lead, quote=True)]
    parts.extend(
        f"{escape(metric.label, quote=True)}: <b>{escape(metric.value, quote=True)}</b>"
        for metric in screen.metrics
    )
    parts.extend(escape(item, quote=True) for item in screen.items)
    if screen.details:
        summary, detail = screen.details
        parts.append(
            f"<blockquote expandable><b>{escape(summary, quote=True)}</b>\n"
            f"{escape(detail, quote=True)}</blockquote>"
        )
    return "\n\n".join(parts)


def build_compact_markup(
    screen: Screen,
    reply_markup: InlineKeyboardMarkup | None,
) -> InlineKeyboardMarkup | None:
    rows = [
        [
            InlineKeyboardButton(
                text=control.label,
                copy_text=CopyTextButton(text=control.value),
            )
        ]
        for control in screen.copy_controls
    ]
    if reply_markup:
        rows.extend(
            [button.model_copy(update={"icon_custom_emoji_id": None}) for button in row]
            for row in reply_markup.inline_keyboard
        )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def build_send_method(
    chat_id: int,
    screen: Screen,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> SendRichMessage:
    return SendRichMessage(
        chat_id=chat_id,
        rich_message=build_rich_message(screen),
        reply_markup=reply_markup,
    )


def build_edit_method(
    chat_id: int,
    message_id: int,
    screen: Screen,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> EditMessageText:
    return EditMessageText(
        chat_id=chat_id,
        message_id=message_id,
        rich_message=build_rich_message(screen),
        reply_markup=reply_markup,
    )


async def send_screen(
    bot: object,
    chat_id: int,
    screen: Screen,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> object:
    try:
        return await bot(build_send_method(chat_id, screen, reply_markup))  # type: ignore[operator]
    except TelegramBadRequest:
        log.exception("rich_screen_rejected title=%s", screen.title)
        return await bot(  # type: ignore[operator]
            SendMessage(
                chat_id=chat_id,
                text=render_compact(screen),
                parse_mode="HTML",
                reply_markup=build_compact_markup(screen, reply_markup),
            )
        )


async def show_screen(
    event: Message | CallbackQuery,
    screen: Screen,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> object | None:
    current = event.message if isinstance(event, CallbackQuery) else event
    if not isinstance(current, Message):
        return None
    if isinstance(event, CallbackQuery):
        await event.answer()

    try:
        if isinstance(event, CallbackQuery) and current.rich_message is not None:
            return await event.bot(
                build_edit_method(current.chat.id, current.message_id, screen, reply_markup)
            )
        sent = await event.bot(build_send_method(current.chat.id, screen, reply_markup))
        if isinstance(event, CallbackQuery):
            with contextlib.suppress(TelegramBadRequest):
                await current.delete()
        return sent
    except TelegramBadRequest as error:
        if "message is not modified" in str(error).lower():
            return current
        log.exception("rich_screen_rejected title=%s", screen.title)

    compact_markup = build_compact_markup(screen, reply_markup)
    if isinstance(event, CallbackQuery) and current.rich_message is not None:
        return await event.bot(
            EditMessageText(
                chat_id=current.chat.id,
                message_id=current.message_id,
                text=render_compact(screen),
                parse_mode="HTML",
                reply_markup=compact_markup,
            )
        )
    sent = await event.bot(
        SendMessage(
            chat_id=current.chat.id,
            text=render_compact(screen),
            parse_mode="HTML",
            reply_markup=compact_markup,
        )
    )
    if isinstance(event, CallbackQuery):
        with contextlib.suppress(TelegramBadRequest):
            await current.delete()
    return sent
