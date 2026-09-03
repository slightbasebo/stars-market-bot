import asyncio
from datetime import datetime, timezone

from aiogram.methods import SendRichMessage
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from stars_market_bot.ui import (
    CopyControl,
    Metric,
    Screen,
    build_compact_markup,
    build_edit_method,
    build_rich_message,
    build_send_method,
    render_compact,
    send_screen,
    show_screen,
)


def test_banner_screen_uses_rich_transport_with_bound_local_media():
    screen = Screen(
        title="Buy Stars",
        lead="Choose an amount.",
        emoji="gift",
        banner="stars.png",
    )

    method = build_send_method(42, screen)

    assert isinstance(method, SendRichMessage)
    assert method.rich_message.media is not None
    assert 'src="tg://photo?id=banner"' in method.rich_message.html
    assert isinstance(method.rich_message.media[0].media.media, FSInputFile)
    assert not method.rich_message.media[0].media.media.path.startswith(("http://", "https://"))


def test_text_screen_is_semantic_and_has_no_media():
    screen = Screen(
        title="Confirm order",
        lead="Check the details before payment.",
        emoji="confirm",
        metrics=(
            Metric("Product", "100 Stars"),
            Metric("Recipient", "@alice"),
            Metric("Payment", "1.1 GRAM"),
        ),
        details=("What happens next", "The bot will create an invoice."),
    )

    rich = build_rich_message(screen)

    assert rich.media is None
    assert "<h1>" in rich.html
    assert "<table bordered striped>" in rich.html
    assert "<details>" in rich.html
    assert "@alice" in rich.html


def test_copy_controls_exist_in_rich_and_compact_fallback():
    screen = Screen(
        title="Invoice #7",
        lead="Send the exact amount.",
        emoji="payment",
        copy_controls=(
            CopyControl("Copy address", "UQ-wallet"),
            CopyControl("Copy comment", "SM-ABC"),
        ),
    )

    rich = build_rich_message(screen)
    compact = render_compact(screen)
    markup = build_compact_markup(screen, None)

    assert rich.html.count('type="copy_text"') == 2
    assert 'text="UQ-wallet"' in rich.html
    assert "Invoice #7" in compact
    assert isinstance(markup, InlineKeyboardMarkup)
    assert markup.inline_keyboard[0][0].copy_text.text == "UQ-wallet"
    assert markup.inline_keyboard[1][0].copy_text.text == "SM-ABC"


def test_compact_fallback_strips_only_custom_button_icons():
    navigation = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Buy Stars",
                    callback_data="menu:stars",
                    icon_custom_emoji_id="5199749070830197566",
                    style="primary",
                )
            ]
        ]
    )

    markup = build_compact_markup(Screen("Store", "Choose.", "hello"), navigation)

    button = markup.inline_keyboard[0][0]
    assert button.icon_custom_emoji_id is None
    assert button.style == "primary"
    assert button.callback_data == "menu:stars"


def test_rich_edit_preserves_document_transport():
    screen = Screen("Buy Premium", "Choose duration.", "premium")

    method = build_edit_method(42, 9, screen)

    assert method.text is None
    assert method.rich_message.media is None
    assert "Buy Premium" in method.rich_message.html


def test_show_screen_sends_rich_message_as_primary_transport():
    class BotCapture:
        def __init__(self):
            self.methods = []

        async def __call__(self, method):
            self.methods.append(method)
            return method

    async def scenario():
        bot = BotCapture()
        message = Message.model_validate(
            {
                "message_id": 1,
                "date": datetime.now(timezone.utc),
                "chat": {"id": 42, "type": "private"},
            },
            context={"bot": bot},
        )
        await show_screen(message, Screen("Store", "Choose a product.", "hello"))
        assert len(bot.methods) == 1
        assert isinstance(bot.methods[0], SendRichMessage)

    asyncio.run(scenario())


def test_send_screen_uses_rich_transport_without_an_incoming_message():
    class BotCapture:
        def __init__(self):
            self.methods = []

        async def __call__(self, method):
            self.methods.append(method)
            return method

    async def scenario():
        bot = BotCapture()
        await send_screen(bot, 42, Screen("Done", "Order completed.", "success"))
        assert len(bot.methods) == 1
        assert isinstance(bot.methods[0], SendRichMessage)

    asyncio.run(scenario())
