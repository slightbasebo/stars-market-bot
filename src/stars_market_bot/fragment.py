import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

from fragment_api import (
    AvailabilityCheck,
    ConflictError,
    FragmentAPI,
    FragmentAPIError,
    PriceQuote,
    RateLimitError,
    ServiceUnavailableError,
)

from .db import OrderRecord
from .domain import Asset, Product, normalize_username


_PURCHASE_STATUSES = frozenset(
    {
        "queued",
        "preparing",
        "sending",
        "submitted",
        "confirming",
        "completed",
        "failed",
        "reconciliation_required",
    }
)
_TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "reconciliation_required"}
)
_SAFE_CODES = frozenset(
    {
        "AVAILABILITY_BUSY",
        "AVAILABILITY_RATE_LIMITED",
        "AVAILABILITY_TEMPORARILY_UNAVAILABLE",
        "INVALID_AVAILABILITY_REQUEST",
        "INVALID_BIP39_SEED",
        "PAYMENT_METHOD_UNAVAILABLE",
        "TOP_UP_REQUIRED",
        "UNSUPPORTED_WALLET_SEED_FORMAT",
        "WALLET_INDEX_NOT_FOUND",
        "WALLET_RESOLVER_BUSY",
        "WALLET_SELECTION_REQUIRED",
    }
)
_SAFE_ACTIONS = frozenset(
    {
        "Check all 12 words and their order",
        "Retry shortly before accepting payment.",
        "Retry the check shortly before accepting payment.",
        "Use payment_method=gram.",
        "Wait before checking another purchase.",
        "Wait briefly and retry the same resolve request.",
    }
)


class _FragmentAdapterError(RuntimeError):
    def __init__(
        self,
        public_message: str,
        *,
        code: str | None = None,
        action: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(public_message)
        self.public_message = public_message
        self.code = code
        self.action = action
        self.retry_after = retry_after


class FragmentTemporaryError(_FragmentAdapterError):
    """A safe, retryable Fragment API failure."""


class FragmentPermanentError(_FragmentAdapterError):
    """A safe Fragment API failure that retrying cannot resolve."""


@dataclass(frozen=True)
class FragmentPurchase:
    purchase_id: str
    status: str
    transaction_hash: str | None = None
    error_code: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES

    @property
    def completed(self) -> bool:
        return self.status == "completed"


@dataclass(frozen=True)
class _Failure:
    kind: str
    public_message: str
    code: str | None = None
    action: str | None = None
    retry_after: int | None = None


@dataclass(frozen=True)
class _CallOutcome:
    value: object | None = None
    failure: _Failure | None = None


def _safe_code(value: object) -> str | None:
    if isinstance(value, str) and value in _SAFE_CODES:
        return value
    return None


def _safe_action(value: object) -> str | None:
    if isinstance(value, str) and value in _SAFE_ACTIONS:
        return value
    return None


def _positive_retry_after(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _failure_from_error(error: BaseException) -> _Failure:
    code = _safe_code(getattr(error, "code", None))
    action = _safe_action(getattr(error, "action", None))
    retry_after = _positive_retry_after(getattr(error, "retry_after", None))
    status_code = getattr(error, "status_code", None)
    if isinstance(error, RateLimitError) or status_code == 429:
        return _Failure(
            "temporary",
            "Fragment service is busy. Try again later.",
            code,
            action,
            retry_after,
        )
    if (
        isinstance(error, ServiceUnavailableError)
        or isinstance(error, (TimeoutError, ConnectionError, OSError))
        or (
            isinstance(error, FragmentAPIError)
            and (status_code is None or status_code >= 500)
        )
    ):
        return _Failure(
            "temporary",
            "Fragment service is temporarily unavailable.",
            code,
            action,
            retry_after,
        )
    if isinstance(error, ConflictError):
        return _Failure(
            "permanent",
            "Fragment request conflicts with an existing purchase.",
            code,
            action,
            retry_after,
        )
    return _Failure(
        "permanent",
        "Fragment request was rejected.",
        code,
        action,
        retry_after,
    )


def _invalid_input() -> _Failure:
    return _Failure(
        "permanent",
        "Fragment request was rejected.",
        "INVALID_INPUT",
    )


def _invalid_response() -> _Failure:
    return _Failure(
        "permanent",
        "Fragment service returned an invalid response.",
        "INVALID_RESPONSE",
    )


def _sdk_username(value: str) -> tuple[str | None, _Failure | None]:
    try:
        return f"@{normalize_username(value)}", None
    except (TypeError, ValueError):
        return None, _invalid_input()


def _order_amount(order: OrderRecord) -> tuple[int | None, _Failure | None]:
    if order.product is Product.STARS and order.product_amount is not None:
        return order.product_amount, None
    if order.product is Product.PREMIUM and order.months is not None:
        return order.months, None
    return None, _invalid_input()


def _purchase_result(
    value: object,
    expected_id: str | None = None,
) -> tuple[FragmentPurchase | None, _Failure | None]:
    purchase_id = getattr(value, "purchase_id", None)
    status = getattr(value, "status", None)
    transaction_hash = getattr(value, "transaction_hash", None)
    raw_error = getattr(value, "error", None)
    error_code = (
        _safe_code(raw_error.get("code"))
        if isinstance(raw_error, Mapping)
        else None
    )
    if (
        not isinstance(purchase_id, str)
        or not purchase_id
        or (expected_id is not None and purchase_id != expected_id)
        or not isinstance(status, str)
        or status not in _PURCHASE_STATUSES
        or (transaction_hash is not None and not isinstance(transaction_hash, str))
    ):
        return None, _invalid_response()
    return (
        FragmentPurchase(
            purchase_id=purchase_id,
            status=status,
            transaction_hash=transaction_hash,
            error_code=error_code,
        ),
        None,
    )


def _raise_public_failure(
    kind: str,
    public_message: str,
    code: str | None,
    action: str | None,
    retry_after: int | None,
) -> None:
    if kind == "temporary":
        raise FragmentTemporaryError(
            public_message,
            code=code,
            action=action,
            retry_after=retry_after,
        )
    raise FragmentPermanentError(
        public_message,
        code=code,
        action=action,
        retry_after=retry_after,
    )


async def _call_sdk(function: Callable[..., Any], /, *args, **kwargs) -> _CallOutcome:
    value = None
    failure = None
    try:
        value = await asyncio.to_thread(function, *args, **kwargs)
    except asyncio.CancelledError:
        raise
    except (
        FragmentAPIError,
        TimeoutError,
        ConnectionError,
        OSError,
    ) as error:
        failure = _failure_from_error(error)
        error = None
    finally:
        function = None
        args = ()
        kwargs = None
    if failure is not None:
        value = None
        return _CallOutcome(failure=failure)
    outcome = _CallOutcome(value=value)
    value = None
    return outcome


class FragmentClient:
    def __init__(self, api: FragmentAPI) -> None:
        self._api = api

    async def check(
        self,
        product: Product,
        username: str,
        amount: int,
        asset: Asset,
    ) -> AvailabilityCheck:
        sdk_username, failure = _sdk_username(username)
        outcome = None
        result = None
        if failure is None:
            outcome = await _call_sdk(
                self._api.check_availability,
                product=product.value,
                username=sdk_username,
                amount=amount,
                payment_method=asset.value,
            )
            result = outcome.value
            failure = outcome.failure
        if failure is None and not isinstance(result, AvailabilityCheck):
            failure = _invalid_response()
        if failure is not None:
            kind = failure.kind
            public_message = failure.public_message
            code = failure.code
            action = failure.action
            retry_after = failure.retry_after
            self = None
            product = None
            username = None
            amount = None
            asset = None
            sdk_username = None
            outcome = None
            result = None
            failure = None
            _raise_public_failure(kind, public_message, code, action, retry_after)
        return result

    async def quote(self, product: Product, amount: int) -> PriceQuote:
        outcome = await _call_sdk(self._api.get_price, product.value, amount)
        result = outcome.value
        failure = outcome.failure
        if failure is None and not isinstance(result, PriceQuote):
            failure = _invalid_response()
        if failure is not None:
            kind = failure.kind
            public_message = failure.public_message
            code = failure.code
            action = failure.action
            retry_after = failure.retry_after
            self = None
            product = None
            amount = None
            outcome = None
            result = None
            failure = None
            _raise_public_failure(kind, public_message, code, action, retry_after)
        return result

    async def create(
        self,
        order: OrderRecord,
        seed: str,
        wallet_address: str,
    ) -> FragmentPurchase:
        sdk_username, failure = _sdk_username(order.recipient)
        amount = None
        if failure is None:
            amount, failure = _order_amount(order)
        outcome = None
        raw_purchase = None
        if failure is None:
            outcome = await _call_sdk(
                self._api.create_purchase,
                product=order.product.value,
                username=sdk_username,
                amount=amount,
                payment_method=order.asset.value,
                seed=seed,
                idempotency_key=order.idempotency_key,
                wallet_address=wallet_address,
            )
            raw_purchase = outcome.value
            failure = outcome.failure
        purchase = None
        if failure is None:
            purchase, failure = _purchase_result(raw_purchase)
        if failure is not None:
            kind = failure.kind
            public_message = failure.public_message
            code = failure.code
            action = failure.action
            retry_after = failure.retry_after
            self = None
            order = None
            seed = None
            wallet_address = None
            sdk_username = None
            amount = None
            outcome = None
            raw_purchase = None
            purchase = None
            failure = None
            _raise_public_failure(kind, public_message, code, action, retry_after)
        return purchase

    async def status(self, purchase_id: str) -> FragmentPurchase:
        outcome = await _call_sdk(self._api.get_purchase, purchase_id)
        raw_purchase = outcome.value
        failure = outcome.failure
        purchase = None
        if failure is None:
            purchase, failure = _purchase_result(
                raw_purchase,
                expected_id=purchase_id,
            )
        if failure is not None:
            kind = failure.kind
            public_message = failure.public_message
            code = failure.code
            action = failure.action
            retry_after = failure.retry_after
            self = None
            purchase_id = None
            outcome = None
            raw_purchase = None
            purchase = None
            failure = None
            _raise_public_failure(kind, public_message, code, action, retry_after)
        return purchase


__all__ = [
    "FragmentClient",
    "FragmentPermanentError",
    "FragmentPurchase",
    "FragmentTemporaryError",
]
