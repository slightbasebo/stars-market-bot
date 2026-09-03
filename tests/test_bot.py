import asyncio
from datetime import datetime, timezone

from stars_market_bot.bot import (
    all_callback_samples,
    confirmation_screen,
    home_screen,
    invoice_screen,
    main_menu_keyboard,
    payment_asset_ready,
    product_screen,
    recipient_keyboard,
)
from stars_market_bot.domain import Asset, Invoice, Product
from stars_market_bot.texts import Language, TEXTS, text


def test_every_language_has_the_same_complete_keys():
    expected = set(TEXTS[Language.RU])
    assert expected
    assert all(set(TEXTS[language]) == expected for language in Language)
    for required in ("buy_stars", "buy_premium", "invoice", "completed", "api_promo"):
        assert required in expected


def test_for_me_is_hidden_without_public_username():
    keyboard = recipient_keyboard(Language.EN, has_username=False)
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert text(Language.EN, "for_me") not in labels


def test_main_menu_and_callbacks_are_telegram_safe():
    keyboard = main_menu_keyboard(Language.UK)
    assert len(keyboard.inline_keyboard) >= 3
    assert all(len(value.encode()) <= 64 for value in all_callback_samples())


def test_main_menu_buttons_have_readable_labels_and_custom_emoji_icons():
    keyboard = main_menu_keyboard(Language.RU)
    buttons = [button for row in keyboard.inline_keyboard for button in row]

    assert all(button.text.strip() for button in buttons)
    assert all(button.icon_custom_emoji_id for button in buttons)
    assert buttons[0].icon_custom_emoji_id == "5199749070830197566"
    assert buttons[1].icon_custom_emoji_id == "5467406098367521267"
    assert buttons[0].style == "primary"
    assert buttons[1].style == "primary"


def test_text_formats_named_values():
    assert "@alice" in text(Language.EN, "recipient_ok", username="@alice")


def test_invoice_displays_destination_and_reference_in_every_language():
    for language in Language:
        value = text(
            language,
            "invoice",
            order_id=7,
            price="1.25",
            asset="GRAM",
            address="UQ-wallet",
            reference="SM-ABC",
        )
        assert "UQ-wallet" in value
        assert "SM-ABC" in value
        assert text(language, "state_completed") != "completed" or language is Language.EN


def test_navigation_screens_use_the_three_product_banners():
    assert home_screen(Language.EN).banner == "home.png"
    assert product_screen(Language.EN, Product.STARS).banner == "stars.png"
    assert product_screen(Language.EN, Product.PREMIUM).banner == "premium.png"


def test_confirmation_screen_shows_the_order_at_a_glance():
    screen = confirmation_screen(
        Language.EN,
        product="100 Stars",
        recipient="@alice",
        price="1.1 GRAM",
    )

    assert [(metric.label, metric.value) for metric in screen.metrics] == [
        ("Product", "100 Stars"),
        ("Recipient", "@alice"),
        ("Payment", "1.1 GRAM"),
    ]


def test_invoice_screen_makes_address_comment_and_order_id_copyable():
    invoice = Invoice(
        destination="UQ-wallet",
        asset=Asset.GRAM,
        units=1_100_000_000,
        reference="SM-ABC",
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc),
    )

    screen = invoice_screen(Language.EN, order_id=7, price="1.1", invoice=invoice)

    assert [control.value for control in screen.copy_controls] == ["UQ-wallet", "SM-ABC", "7"]


def test_usdt_requires_owner_gas_but_gram_does_not():
    checks = []

    async def unavailable():
        checks.append(True)
        return False

    assert asyncio.run(payment_asset_ready(Asset.USDT, unavailable)) is False
    assert asyncio.run(payment_asset_ready(Asset.GRAM, unavailable)) is True
    assert checks == [True]


def test_usdt_is_disabled_when_balance_check_fails():
    async def broken_check():
        raise RuntimeError("TON Center is unavailable")

    assert asyncio.run(payment_asset_ready(Asset.USDT, broken_check)) is False
