from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
import re
from types import MappingProxyType
from urllib.parse import quote, urlencode


CANONICAL_USDT_MASTER = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"


class Asset(str, Enum):
    GRAM = "gram"
    USDT = "usdt"


class Product(str, Enum):
    STARS = "stars"
    PREMIUM = "premium"


class OrderState(str, Enum):
    DRAFT = "draft"
    AWAITING_PAYMENT = "awaiting_payment"
    PAID = "paid"
    PURCHASING = "purchasing"
    COMPLETED = "completed"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    MANUAL_REVIEW = "manual_review"
    EXPIRED = "expired"


class MatchResult(str, Enum):
    MATCH = "match"
    WRONG_DESTINATION = "wrong_destination"
    WRONG_ASSET = "wrong_asset"
    WRONG_JETTON_MASTER = "wrong_jetton_master"
    WRONG_AMOUNT = "wrong_amount"
    WRONG_REFERENCE = "wrong_reference"
    OUTSIDE_INVOICE_WINDOW = "outside_invoice_window"
    NOT_FINAL = "not_final"
    ABORTED = "aborted"
    BOUNCED = "bounced"


ASSET_DECIMALS = {Asset.GRAM: 9, Asset.USDT: 6}

_TRANSITIONS = MappingProxyType(
    {
        OrderState.DRAFT: frozenset({OrderState.AWAITING_PAYMENT}),
        OrderState.AWAITING_PAYMENT: frozenset(
            {
                OrderState.PAID,
                OrderState.EXPIRED,
                OrderState.MANUAL_REVIEW,
            }
        ),
        OrderState.PAID: frozenset(
            {OrderState.PURCHASING, OrderState.MANUAL_REVIEW}
        ),
        OrderState.PURCHASING: frozenset(
            {
                OrderState.COMPLETED,
                OrderState.FAILED,
                OrderState.RECONCILIATION_REQUIRED,
                OrderState.MANUAL_REVIEW,
            }
        ),
        OrderState.RECONCILIATION_REQUIRED: frozenset(
            {OrderState.COMPLETED, OrderState.FAILED}
        ),
    }
)

_USERNAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{4,31}\Z")


def _require_asset(asset: Asset) -> None:
    if not isinstance(asset, Asset):
        raise ValueError("asset must be an Asset")


def _require_positive_units(units: int) -> None:
    if type(units) is not int or units <= 0:
        raise ValueError("units must be a positive integer")


def _require_nonempty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


def _require_utc(value: datetime, field_name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValueError(f"{field_name} must be a timezone-aware UTC datetime")


@dataclass(frozen=True)
class Money:
    asset: Asset
    units: int

    def __post_init__(self) -> None:
        _require_asset(self.asset)
        _require_positive_units(self.units)


@dataclass(frozen=True)
class Invoice:
    destination: str
    asset: Asset
    units: int
    reference: str
    created_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        _require_nonempty(self.destination, "destination")
        _require_asset(self.asset)
        _require_positive_units(self.units)
        _require_nonempty(self.reference, "reference")
        _require_utc(self.created_at, "created_at")
        _require_utc(self.expires_at, "expires_at")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")


@dataclass(frozen=True)
class PaymentCandidate:
    tx_hash: str
    logical_time: int
    destination: str
    asset: Asset
    units: int
    comment: str | None
    timestamp: datetime
    finalized: bool
    aborted: bool
    bounced: bool
    jetton_master: str | None = None

    def __post_init__(self) -> None:
        _require_nonempty(self.tx_hash, "tx_hash")
        if type(self.logical_time) is not int or self.logical_time < 0:
            raise ValueError("logical_time must be a non-negative integer")
        _require_nonempty(self.destination, "destination")
        _require_asset(self.asset)
        _require_positive_units(self.units)
        if self.comment is not None and not isinstance(self.comment, str):
            raise ValueError("comment must be a string or None")
        _require_utc(self.timestamp, "timestamp")
        for field_name in ("finalized", "aborted", "bounced"):
            if type(getattr(self, field_name)) is not bool:
                raise ValueError(f"{field_name} must be a boolean")
        if self.jetton_master is not None:
            _require_nonempty(self.jetton_master, "jetton_master")


def normalize_username(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("username must be a string")
    username = value[1:] if value.startswith("@") else value
    if _USERNAME_PATTERN.fullmatch(username) is None:
        raise ValueError("username must be 5-32 ASCII letters, digits, or underscores")
    return username


def quote_customer_amount(
    api_amount: str, commission: Decimal, asset: Asset
) -> Money:
    if not isinstance(api_amount, str):
        raise TypeError("api_amount must be a decimal string")
    if not isinstance(commission, Decimal):
        raise TypeError("commission must be a Decimal")
    _require_asset(asset)

    try:
        amount = Decimal(api_amount)
    except InvalidOperation as exc:
        raise ValueError("api_amount must be a valid decimal") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("api_amount must be a positive finite decimal")
    if (
        not commission.is_finite()
        or commission < Decimal("0")
        or commission > Decimal("100")
    ):
        raise ValueError("commission must be from 0 through 100")

    amount_numerator, amount_denominator = amount.as_integer_ratio()
    commission_numerator, commission_denominator = commission.as_integer_ratio()
    numerator = (
        amount_numerator
        * (100 * commission_denominator + commission_numerator)
        * 10 ** ASSET_DECIMALS[asset]
    )
    denominator = amount_denominator * 100 * commission_denominator
    units = -(-numerator // denominator)
    return Money(asset=asset, units=units)


def build_payment_link(invoice: Invoice) -> str:
    query = {
        "amount": str(invoice.units),
        "text": invoice.reference,
        "exp": str(int(invoice.expires_at.timestamp())),
    }
    if invoice.asset is Asset.USDT:
        query["jetton"] = CANONICAL_USDT_MASTER
    return f"ton://transfer/{quote(invoice.destination, safe='')}?{urlencode(query)}"


def match_payment(invoice: Invoice, candidate: PaymentCandidate) -> MatchResult:
    if candidate.destination != invoice.destination:
        return MatchResult.WRONG_DESTINATION
    if candidate.asset is not invoice.asset:
        return MatchResult.WRONG_ASSET
    if invoice.asset is Asset.USDT:
        if candidate.jetton_master != CANONICAL_USDT_MASTER:
            return MatchResult.WRONG_JETTON_MASTER
    elif candidate.jetton_master is not None:
        return MatchResult.WRONG_JETTON_MASTER
    if candidate.units != invoice.units:
        return MatchResult.WRONG_AMOUNT
    if candidate.comment != invoice.reference:
        return MatchResult.WRONG_REFERENCE
    if not invoice.created_at <= candidate.timestamp <= invoice.expires_at:
        return MatchResult.OUTSIDE_INVOICE_WINDOW
    if not candidate.finalized:
        return MatchResult.NOT_FINAL
    if candidate.aborted:
        return MatchResult.ABORTED
    if candidate.bounced:
        return MatchResult.BOUNCED
    return MatchResult.MATCH


def can_transition(current: OrderState, target: OrderState) -> bool:
    return target in _TRANSITIONS.get(current, frozenset())
