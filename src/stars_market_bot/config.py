from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _required(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise ValueError(f"{key} must be non-empty")
    return value


def _positive_int(env: Mapping[str, str], key: str) -> int:
    value = _required(env, key)
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{key} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{key} must be positive")
    return parsed


@dataclass(frozen=True)
class Settings:
    bot_token: str
    owner_seed: str
    owner_wallet_address: str
    owner_telegram_id: int
    commission_percent: Decimal
    database_path: Path
    public_base_url: str
    webhook_path: str
    webhook_secret: str
    fragment_api_url: str
    toncenter_api_url: str
    scan_interval_seconds: int
    invoice_ttl_seconds: int = 900
    webhook_certificate_path: Path | None = None
    delivery_mode: str = "webhook"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        owner_seed = _required(env, "OWNER_SEED_24W")
        if len(owner_seed.split()) != 24:
            raise ValueError("OWNER_SEED_24W must contain 24 words")

        commission_text = env.get("COMISSION_PERCENT", "10").strip()
        try:
            commission = Decimal(commission_text)
        except InvalidOperation as exc:
            raise ValueError("COMISSION_PERCENT must be from 0 through 100") from exc
        if not commission.is_finite() or not Decimal("0") <= commission <= Decimal("100"):
            raise ValueError("COMISSION_PERCENT must be from 0 through 100")

        delivery_mode = env.get("DELIVERY_MODE", "webhook").strip().lower()
        if delivery_mode not in {"polling", "webhook"}:
            raise ValueError("DELIVERY_MODE must be polling or webhook")

        public_base_url = env.get("PUBLIC_BASE_URL", "").strip()
        webhook_path = env.get("WEBHOOK_PATH", "").strip()
        webhook_secret = env.get("WEBHOOK_SECRET", "").strip()
        if delivery_mode == "webhook":
            public_base_url = _required(env, "PUBLIC_BASE_URL")
            parsed_public_url = urlparse(public_base_url)
            if parsed_public_url.scheme.lower() != "https":
                raise ValueError("PUBLIC_BASE_URL must use HTTPS")
            if parsed_public_url.hostname is None:
                raise ValueError("PUBLIC_BASE_URL must include a hostname")
            webhook_path = _required(env, "WEBHOOK_PATH")
            webhook_secret = _required(env, "WEBHOOK_SECRET")
            if not webhook_path.startswith("/"):
                raise ValueError("WEBHOOK_PATH must start with /")

        invoice_ttl = int(env.get("INVOICE_TTL_SECONDS", "900"))
        if invoice_ttl <= 0:
            raise ValueError("INVOICE_TTL_SECONDS must be positive")

        return cls(
            bot_token=_required(env, "BOT_TOKEN"),
            owner_seed=owner_seed,
            owner_wallet_address=_required(env, "OWNER_WALLET_ADDRESS"),
            owner_telegram_id=_positive_int(env, "OWNER_TELEGRAM_ID"),
            commission_percent=commission,
            database_path=Path(_required(env, "DATABASE_PATH")),
            public_base_url=public_base_url.rstrip("/"),
            webhook_path=webhook_path,
            webhook_secret=webhook_secret,
            fragment_api_url=_required(env, "FRAGMENT_API_URL").rstrip("/"),
            toncenter_api_url=_required(env, "TONCENTER_API_URL").rstrip("/"),
            scan_interval_seconds=_poll_interval(env),
            invoice_ttl_seconds=invoice_ttl,
            webhook_certificate_path=(
                Path(env["WEBHOOK_CERT_PATH"])
                if env.get("WEBHOOK_CERT_PATH", "").strip()
                else None
            ),
            delivery_mode=delivery_mode,
        )


def _poll_interval(env: Mapping[str, str]) -> int:
    raw_value = _required(env, "SCAN_INTERVAL_SECONDS")
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError("SCAN_INTERVAL_SECONDS must be at least one second") from exc
    if value < 1:
        raise ValueError("SCAN_INTERVAL_SECONDS must be at least one second")
    return value
