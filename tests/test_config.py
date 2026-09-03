from decimal import Decimal
from pathlib import Path

import pytest

from stars_market_bot.config import Settings, load_env_file


SEED = "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty twentyone twentytwo twentythree twentyfour"


def valid_env() -> dict[str, str]:
    return {
        "BOT_TOKEN": "123456:telegram-token",
        "OWNER_SEED_24W": SEED,
        "OWNER_WALLET_ADDRESS": "EQD-owner-wallet",
        "OWNER_TELEGRAM_ID": "8670053970",
        "COMISSION_PERCENT": "10",
        "DATABASE_PATH": "/var/lib/stars-market-bot/bot.sqlite3",
        "PUBLIC_BASE_URL": "https://starsbot.129.213.144.127.sslip.io",
        "WEBHOOK_PATH": "/telegram/webhook",
        "WEBHOOK_SECRET": "webhook-secret",
        "FRAGMENT_API_URL": "https://api-stars.duckdns.org",
        "TONCENTER_API_URL": "https://toncenter.com/api/v3",
        "SCAN_INTERVAL_SECONDS": "5",
        "INVOICE_TTL_SECONDS": "900",
    }


def test_settings_accept_production_contract():
    settings = Settings.from_env(valid_env())

    assert settings.owner_telegram_id == 8670053970
    assert settings.commission_percent == Decimal("10")
    assert settings.invoice_ttl_seconds == 900


def test_settings_reject_http_public_url():
    env = valid_env() | {"PUBLIC_BASE_URL": "http://example.test"}

    with pytest.raises(ValueError, match="HTTPS"):
        Settings.from_env(env)


def test_settings_defaults_missing_commission_to_ten_percent():
    env = valid_env()
    del env["COMISSION_PERCENT"]

    settings = Settings.from_env(env)

    assert settings.commission_percent == Decimal("10")


def test_polling_mode_does_not_require_webhook_settings():
    env = valid_env() | {"DELIVERY_MODE": "polling"}
    del env["PUBLIC_BASE_URL"]
    del env["WEBHOOK_PATH"]
    del env["WEBHOOK_SECRET"]

    settings = Settings.from_env(env)

    assert settings.delivery_mode == "polling"
    assert settings.public_base_url == ""


def test_settings_reject_https_public_url_without_hostname():
    env = valid_env() | {"PUBLIC_BASE_URL": "https:///missing-host"}

    with pytest.raises(ValueError, match="hostname"):
        Settings.from_env(env)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("OWNER_TELEGRAM_ID", "0", "positive"),
        ("OWNER_SEED_24W", "one two", "24 words"),
        ("COMISSION_PERCENT", "101", "0 through 100"),
        ("WEBHOOK_SECRET", "", "non-empty"),
        ("WEBHOOK_PATH", "webhook", "start with /"),
        ("SCAN_INTERVAL_SECONDS", "0", "at least one second"),
    ],
)
def test_settings_reject_invalid_values(key, value, message):
    with pytest.raises(ValueError, match=message):
        Settings.from_env(valid_env() | {key: value})


def test_load_env_file_preserves_quoted_seed_and_splits_first_equals(tmp_path: Path):
    env_file = tmp_path / ".env.example"
    env_file.write_text(
        'OWNER_SEED_24W="one two three"\n'
        "WEBHOOK_SECRET=part=two\n"
        "# ignored\n"
        "EMPTY=\n",
        encoding="utf-8",
    )

    assert load_env_file(env_file) == {
        "OWNER_SEED_24W": "one two three",
        "WEBHOOK_SECRET": "part=two",
        "EMPTY": "",
    }
