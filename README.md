# Stars Market Bot

Reference Telegram shop built with aiogram 3. It sells Telegram Stars and
Premium through the [Fragment Market API](https://stars-market.duckdns.org/api)
and accepts exact GRAM or official USDT payments on TON.

## Features

- Russian, English, Ukrainian, and Turkish interface.
- Native Telegram Rich Message screens with local banners and custom emoji.
- Stars presets plus custom amounts from 50 to 1,000,000.
- Premium for 3, 6, or 12 months.
- Recipient availability check before an invoice is created.
- Exact Decimal pricing with a configurable commission (10% by default).
- Fifteen-minute TON invoices with a unique comment.
- Durable SQLite orders, payment deduplication, and Fragment idempotency.
- Telegram long polling, background payment scan, expiry, and purchase recovery.

## Configuration

Copy `.env.example` to `.env` and set every value. Never commit `.env` or paste
the wallet seed into an issue or support chat.

- `BOT_TOKEN`: token from BotFather.
- `OWNER_SEED_24W`: dedicated 24-word TON mnemonic.
- `OWNER_WALLET_ADDRESS`: active V4R2 or V5R1 address derived from that seed.
- `OWNER_TELEGRAM_ID`: owner chat ID for manual-review alerts.
- `COMISSION_PERCENT`: customer markup percentage.
- `DATABASE_PATH`: SQLite file path.
- `DELIVERY_MODE`: `polling` for the simplest deployment, or `webhook`.
- `PUBLIC_BASE_URL`, `WEBHOOK_PATH`, `WEBHOOK_SECRET`: required only in webhook mode.
- `WEBHOOK_CERT_PATH`: optional self-signed webhook certificate.
- `FRAGMENT_API_URL`: normally `https://api-stars.duckdns.org`.
- `TONCENTER_API_URL`: normally `https://toncenter.com/api/v3`.
- `SCAN_INTERVAL_SECONDS`: payment polling interval; keep at least 5 without an API key.
- `INVOICE_TTL_SECONDS`: invoice lifetime, normally `900`.

The same wallet receives customer payments and pays Fragment purchases. Keep a
small GRAM balance available for network fees, including when customers pay in
USDT. Automatic refunds are intentionally not implemented; expired, ambiguous,
or underfunded payments require manual review.

## Local run

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest -q
.venv/bin/python -m stars_market_bot.app
```

The HTTP health endpoint is available only in webhook mode. In the default
polling mode, check the systemd service and logs as shown below.

## Production layout

The included systemd unit runs the app from `/opt/stars-market-bot`, keeps the
database at `/var/lib/stars-market-bot/bot.sqlite3`, and reads secrets from
`/etc/stars-market-bot.env` (mode `0600`). It uses long polling, so no public
port, domain, reverse proxy, or routing changes are required.

Before reload, validate the service:

```bash
systemd-analyze verify /etc/systemd/system/stars-market-bot.service
```

Useful checks:

```bash
systemctl status stars-market-bot --no-pager
journalctl -u stars-market-bot -n 100 --no-pager
```

Bot API `getWebhookInfo` should show an empty webhook URL. Back up SQLite with
`sqlite3 /var/lib/stars-market-bot/bot.sqlite3 '.backup /safe/path/bot.sqlite3'`.
For rollback, point `/opt/stars-market-bot` back to the previous release and
restart only `stars-market-bot`; do not delete the database.

## API

Want your own shop? Use the
[Fragment Market API](https://stars-market.duckdns.org/api). Its SDK and API
source are available at
[slightbasebo/fragment-api-dev](https://github.com/slightbasebo/fragment-api-dev).
