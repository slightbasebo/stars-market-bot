import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from typing import Iterator

from .domain import (
    Asset,
    Invoice,
    MatchResult,
    OrderState,
    PaymentCandidate,
    Product,
    can_transition,
)


SUPPORTED_LANGUAGES = frozenset({"ru", "en", "uk", "tr"})
_DEFAULT_BUSY_TIMEOUT_MS = 5_000


@dataclass(frozen=True)
class OrderRecord:
    id: int
    user_id: int
    product: Product
    recipient: str
    product_amount: int | None
    months: int | None
    asset: Asset
    quoted_api_units: int
    customer_units: int | None
    destination: str | None
    reference: str | None
    invoice_created_at: datetime | None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None
    state: OrderState
    idempotency_key: str
    fragment_purchase_id: str | None
    payment_hash: str | None
    final_transaction_hash: str | None
    error_code: str | None
    error_message: str | None

    @property
    def invoice(self) -> Invoice | None:
        values = (
            self.destination,
            self.customer_units,
            self.reference,
            self.invoice_created_at,
            self.expires_at,
        )
        if all(value is None for value in values):
            return None
        if any(value is None for value in values):
            raise ValueError("stored invoice is incomplete")
        return Invoice(
            destination=self.destination,
            asset=self.asset,
            units=self.customer_units,
            reference=self.reference,
            created_at=self.invoice_created_at,
            expires_at=self.expires_at,
        )


@dataclass(frozen=True)
class PaymentRecord:
    tx_hash: str
    logical_time: int
    destination: str
    asset: Asset
    units: int
    comment: str | None
    timestamp: datetime
    match_result: MatchResult
    jetton_master: str | None
    order_id: int | None
    credited: bool


@dataclass(frozen=True)
class ScannerCursor:
    stream_key: str
    logical_time: int
    tx_hash: str


def _require_positive_int(value: int, field_name: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")


def _require_optional_positive_int(value: int | None, field_name: str) -> None:
    if value is not None:
        _require_positive_int(value, field_name)


def _require_nonempty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


def _require_optional_string(value: str | None, field_name: str) -> None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string or None")


def _datetime_text(value: datetime, field_name: str) -> str:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValueError(f"{field_name} must be a timezone-aware UTC datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _datetime_from_text(value: str | None) -> datetime | None:
    if value is None:
        return None
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() != timedelta(0):
        raise ValueError("stored timestamp is not UTC")
    return result.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_limit(limit: int) -> None:
    if type(limit) is not int or limit < 0:
        raise ValueError("limit must be a non-negative integer")


@contextmanager
def _immediate_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


class Repository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._lock = asyncio.Lock()
        self._closed = False

    @classmethod
    async def open(cls, path: Path) -> "Repository":
        if not isinstance(path, Path):
            raise TypeError("path must be a Path")
        connection = sqlite3.connect(path, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {_DEFAULT_BUSY_TIMEOUT_MS}")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL").fetchone()
        except BaseException:
            connection.close()
            raise
        return cls(connection)

    async def setup(self) -> None:
        async with self._lock:
            self._require_open()
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY CHECK (telegram_id > 0),
                    language TEXT CHECK (
                        language IS NULL OR language IN ('ru', 'en', 'uk', 'tr')
                    )
                );

                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
                    product TEXT NOT NULL CHECK (product IN ('stars', 'premium')),
                    recipient TEXT NOT NULL CHECK (length(trim(recipient)) > 0),
                    product_amount INTEGER CHECK (
                        product_amount IS NULL OR product_amount > 0
                    ),
                    months INTEGER CHECK (months IS NULL OR months > 0),
                    asset TEXT NOT NULL CHECK (asset IN ('gram', 'usdt')),
                    quoted_api_units INTEGER NOT NULL CHECK (quoted_api_units > 0),
                    customer_units INTEGER CHECK (
                        customer_units IS NULL OR customer_units > 0
                    ),
                    destination TEXT,
                    reference TEXT,
                    invoice_created_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT,
                    state TEXT NOT NULL CHECK (
                        state IN (
                            'draft',
                            'awaiting_payment',
                            'paid',
                            'purchasing',
                            'completed',
                            'failed',
                            'reconciliation_required',
                            'manual_review',
                            'expired'
                        )
                    ),
                    idempotency_key TEXT NOT NULL UNIQUE,
                    fragment_purchase_id TEXT,
                    payment_hash TEXT,
                    final_transaction_hash TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    CHECK (
                        (product = 'stars' AND product_amount IS NOT NULL AND months IS NULL)
                        OR
                        (product = 'premium' AND product_amount IS NULL AND months IS NOT NULL)
                    ),
                    CHECK (
                        (state = 'draft'
                            AND customer_units IS NULL
                            AND destination IS NULL
                            AND reference IS NULL
                            AND invoice_created_at IS NULL
                            AND expires_at IS NULL)
                        OR
                        (state <> 'draft'
                            AND customer_units IS NOT NULL
                            AND destination IS NOT NULL
                            AND reference IS NOT NULL
                            AND invoice_created_at IS NOT NULL
                            AND expires_at IS NOT NULL)
                    )
                );

                CREATE UNIQUE INDEX IF NOT EXISTS orders_reference_unique
                    ON orders(reference) WHERE reference IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS orders_fragment_purchase_unique
                    ON orders(fragment_purchase_id)
                    WHERE fragment_purchase_id IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS orders_payment_hash_unique
                    ON orders(payment_hash) WHERE payment_hash IS NOT NULL;
                CREATE INDEX IF NOT EXISTS orders_user_updated_idx
                    ON orders(user_id, updated_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS orders_state_created_idx
                    ON orders(state, created_at, id);

                CREATE TABLE IF NOT EXISTS payments (
                    tx_hash TEXT PRIMARY KEY,
                    logical_time INTEGER NOT NULL CHECK (logical_time >= 0),
                    destination TEXT NOT NULL,
                    asset TEXT NOT NULL CHECK (asset IN ('gram', 'usdt')),
                    units INTEGER NOT NULL CHECK (units > 0),
                    comment TEXT,
                    timestamp TEXT NOT NULL,
                    match_result TEXT NOT NULL CHECK (
                        match_result IN (
                            'match',
                            'wrong_destination',
                            'wrong_asset',
                            'wrong_jetton_master',
                            'wrong_amount',
                            'wrong_reference',
                            'outside_invoice_window',
                            'not_final',
                            'aborted',
                            'bounced'
                        )
                    ),
                    jetton_master TEXT,
                    finalized INTEGER NOT NULL CHECK (finalized IN (0, 1)),
                    aborted INTEGER NOT NULL CHECK (aborted IN (0, 1)),
                    bounced INTEGER NOT NULL CHECK (bounced IN (0, 1)),
                    order_id INTEGER REFERENCES orders(id),
                    credited INTEGER NOT NULL DEFAULT 0 CHECK (credited IN (0, 1))
                );

                CREATE UNIQUE INDEX IF NOT EXISTS payments_one_credit_per_order
                    ON payments(order_id) WHERE credited = 1;

                CREATE TABLE IF NOT EXISTS scanner_state (
                    stream_key TEXT PRIMARY KEY,
                    logical_time INTEGER NOT NULL CHECK (logical_time >= 0),
                    tx_hash TEXT NOT NULL
                );
                """
            )

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True

    async def set_language(self, user_id: int, language: str) -> None:
        _require_positive_int(user_id, "user_id")
        if language not in SUPPORTED_LANGUAGES:
            raise ValueError("language must be one of ru, en, uk, tr")
        async with self._lock:
            self._require_open()
            self._connection.execute(
                """
                INSERT INTO users(telegram_id, language)
                VALUES (?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET language = excluded.language
                """,
                (user_id, language),
            )

    async def get_language(self, user_id: int) -> str | None:
        _require_positive_int(user_id, "user_id")
        async with self._lock:
            self._require_open()
            row = self._connection.execute(
                "SELECT language FROM users WHERE telegram_id = ?",
                (user_id,),
            ).fetchone()
            return None if row is None else row["language"]

    async def create_order(
        self,
        *,
        user_id: int,
        product: Product,
        recipient: str,
        product_amount: int | None,
        months: int | None,
        asset: Asset,
        quoted_api_units: int,
        idempotency_key: str,
        created_at: datetime,
    ) -> OrderRecord:
        _require_positive_int(user_id, "user_id")
        if not isinstance(product, Product):
            raise ValueError("product must be a Product")
        _require_nonempty(recipient, "recipient")
        _require_optional_positive_int(product_amount, "product_amount")
        _require_optional_positive_int(months, "months")
        if product is Product.STARS and (product_amount is None or months is not None):
            raise ValueError("Stars orders require product_amount only")
        if product is Product.PREMIUM and (months is None or product_amount is not None):
            raise ValueError("Premium orders require months only")
        if not isinstance(asset, Asset):
            raise ValueError("asset must be an Asset")
        _require_positive_int(quoted_api_units, "quoted_api_units")
        _require_nonempty(idempotency_key, "idempotency_key")
        timestamp = _datetime_text(created_at, "created_at")

        async with self._lock:
            self._require_open()
            with _immediate_transaction(self._connection):
                self._connection.execute(
                    "INSERT INTO users(telegram_id) VALUES (?) ON CONFLICT DO NOTHING",
                    (user_id,),
                )
                cursor = self._connection.execute(
                    """
                    INSERT INTO orders(
                        user_id, product, recipient, product_amount, months, asset,
                        quoted_api_units, created_at, updated_at, state, idempotency_key
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        product.value,
                        recipient,
                        product_amount,
                        months,
                        asset.value,
                        quoted_api_units,
                        timestamp,
                        timestamp,
                        OrderState.DRAFT.value,
                        idempotency_key,
                    ),
                )
                row = self._connection.execute(
                    "SELECT * FROM orders WHERE id = ?",
                    (cursor.lastrowid,),
                ).fetchone()
            return self._order_from_row(row)

    async def set_invoice(
        self,
        order_id: int,
        invoice: Invoice,
        *,
        updated_at: datetime | None = None,
    ) -> bool:
        _require_positive_int(order_id, "order_id")
        if not isinstance(invoice, Invoice):
            raise ValueError("invoice must be an Invoice")
        update_text = _datetime_text(updated_at or invoice.created_at, "updated_at")
        invoice_created_text = _datetime_text(invoice.created_at, "invoice.created_at")
        expires_text = _datetime_text(invoice.expires_at, "invoice.expires_at")

        async with self._lock:
            self._require_open()
            with _immediate_transaction(self._connection):
                existing = self._connection.execute(
                    "SELECT asset FROM orders WHERE id = ?",
                    (order_id,),
                ).fetchone()
                if existing is not None and Asset(existing["asset"]) is not invoice.asset:
                    raise ValueError("invoice asset must match the order asset")
                cursor = self._connection.execute(
                    """
                    UPDATE orders
                    SET customer_units = ?, destination = ?, reference = ?,
                        invoice_created_at = ?, expires_at = ?, updated_at = ?,
                        state = ?
                    WHERE id = ? AND state = ? AND asset = ?
                    """,
                    (
                        invoice.units,
                        invoice.destination,
                        invoice.reference,
                        invoice_created_text,
                        expires_text,
                        update_text,
                        OrderState.AWAITING_PAYMENT.value,
                        order_id,
                        OrderState.DRAFT.value,
                        invoice.asset.value,
                    ),
                )
                return cursor.rowcount == 1

    async def get_order(
        self,
        order_id: int,
        *,
        user_id: int | None = None,
    ) -> OrderRecord | None:
        _require_positive_int(order_id, "order_id")
        if user_id is not None:
            _require_positive_int(user_id, "user_id")
        async with self._lock:
            self._require_open()
            if user_id is None:
                row = self._connection.execute(
                    "SELECT * FROM orders WHERE id = ?",
                    (order_id,),
                ).fetchone()
            else:
                row = self._connection.execute(
                    "SELECT * FROM orders WHERE id = ? AND user_id = ?",
                    (order_id, user_id),
                ).fetchone()
            return None if row is None else self._order_from_row(row)

    async def list_user_orders(
        self,
        user_id: int,
        *,
        limit: int = 20,
    ) -> list[OrderRecord]:
        _require_positive_int(user_id, "user_id")
        _validate_limit(limit)
        if limit == 0:
            return []
        async with self._lock:
            self._require_open()
            rows = self._connection.execute(
                """
                SELECT * FROM orders
                WHERE user_id = ?
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
            return [self._order_from_row(row) for row in rows]

    async def find_invoice_by_reference(
        self,
        reference: str,
    ) -> OrderRecord | None:
        _require_nonempty(reference, "reference")
        async with self._lock:
            self._require_open()
            row = self._connection.execute(
                "SELECT * FROM orders WHERE reference = ?",
                (reference,),
            ).fetchone()
            return None if row is None else self._order_from_row(row)

    async def record_payment(
        self,
        order_id: int | None,
        payment: PaymentCandidate,
        match_result: MatchResult = MatchResult.MATCH,
    ) -> bool:
        if order_id is not None:
            _require_positive_int(order_id, "order_id")
        if not isinstance(payment, PaymentCandidate):
            raise ValueError("payment must be a PaymentCandidate")
        if not isinstance(match_result, MatchResult):
            raise ValueError("match_result must be a MatchResult")
        timestamp = _datetime_text(payment.timestamp, "payment.timestamp")

        async with self._lock:
            self._require_open()
            with _immediate_transaction(self._connection):
                duplicate = self._connection.execute(
                    "SELECT 1 FROM payments WHERE tx_hash = ?",
                    (payment.tx_hash,),
                ).fetchone()
                if duplicate is not None:
                    return False
                self._connection.execute(
                    """
                    INSERT INTO payments(
                        tx_hash, logical_time, destination, asset, units, comment,
                        timestamp, match_result, jetton_master, finalized, aborted,
                        bounced, order_id, credited
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        payment.tx_hash,
                        payment.logical_time,
                        payment.destination,
                        payment.asset.value,
                        payment.units,
                        payment.comment,
                        timestamp,
                        match_result.value,
                        payment.jetton_master,
                        int(payment.finalized),
                        int(payment.aborted),
                        int(payment.bounced),
                        order_id,
                    ),
                )
                if match_result is not MatchResult.MATCH or order_id is None:
                    return True
                cursor = self._connection.execute(
                    """
                    UPDATE orders
                    SET state = ?, payment_hash = ?, updated_at = ?
                    WHERE id = ? AND state = ? AND payment_hash IS NULL
                    """,
                    (
                        OrderState.PAID.value,
                        payment.tx_hash,
                        timestamp,
                        order_id,
                        OrderState.AWAITING_PAYMENT.value,
                    ),
                )
                if cursor.rowcount != 1:
                    return False
                credited = self._connection.execute(
                    """
                    UPDATE payments SET credited = 1
                    WHERE tx_hash = ? AND credited = 0
                    """,
                    (payment.tx_hash,),
                )
                if credited.rowcount != 1:
                    raise sqlite3.IntegrityError("payment credit was not persisted")
                return True

    async def claim_paid_order(
        self,
        *,
        updated_at: datetime | None = None,
    ) -> OrderRecord | None:
        update_text = _datetime_text(updated_at or _utc_now(), "updated_at")
        async with self._lock:
            self._require_open()
            with _immediate_transaction(self._connection):
                selected = self._connection.execute(
                    """
                    SELECT id FROM orders
                    WHERE state = ?
                    ORDER BY created_at ASC, id ASC
                    LIMIT 1
                    """,
                    (OrderState.PAID.value,),
                ).fetchone()
                if selected is None:
                    return None
                cursor = self._connection.execute(
                    """
                    UPDATE orders
                    SET state = ?, updated_at = ?
                    WHERE id = ? AND state = ?
                    """,
                    (
                        OrderState.PURCHASING.value,
                        update_text,
                        selected["id"],
                        OrderState.PAID.value,
                    ),
                )
                if cursor.rowcount != 1:
                    return None
                row = self._connection.execute(
                    "SELECT * FROM orders WHERE id = ?",
                    (selected["id"],),
                ).fetchone()
                return self._order_from_row(row)

    async def retry_paid_order(
        self,
        order_id: int,
        idempotency_key: str,
        *,
        updated_at: datetime | None = None,
    ) -> bool:
        _require_positive_int(order_id, "order_id")
        _require_nonempty(idempotency_key, "idempotency_key")
        update_text = _datetime_text(updated_at or _utc_now(), "updated_at")
        retryable = (
            OrderState.FAILED.value,
            OrderState.MANUAL_REVIEW.value,
            OrderState.RECONCILIATION_REQUIRED.value,
        )
        async with self._lock:
            self._require_open()
            with _immediate_transaction(self._connection):
                cursor = self._connection.execute(
                    """
                    UPDATE orders
                    SET state = ?, idempotency_key = ?, fragment_purchase_id = NULL,
                        final_transaction_hash = NULL, error_code = NULL,
                        error_message = NULL, updated_at = ?
                    WHERE id = ? AND payment_hash IS NOT NULL
                      AND state IN (?, ?, ?)
                    """,
                    (
                        OrderState.PAID.value,
                        idempotency_key,
                        update_text,
                        order_id,
                        *retryable,
                    ),
                )
                return cursor.rowcount == 1

    async def record_fragment_purchase(
        self,
        order_id: int,
        purchase_id: str,
        *,
        updated_at: datetime | None = None,
    ) -> bool:
        _require_positive_int(order_id, "order_id")
        _require_nonempty(purchase_id, "purchase_id")
        update_text = _datetime_text(updated_at or _utc_now(), "updated_at")
        async with self._lock:
            self._require_open()
            with _immediate_transaction(self._connection):
                row = self._connection.execute(
                    """
                    SELECT state, fragment_purchase_id FROM orders WHERE id = ?
                    """,
                    (order_id,),
                ).fetchone()
                if row is None or OrderState(row["state"]) is not OrderState.PURCHASING:
                    return False
                existing = row["fragment_purchase_id"]
                if existing is not None:
                    return existing == purchase_id
                cursor = self._connection.execute(
                    """
                    UPDATE orders
                    SET fragment_purchase_id = ?, updated_at = ?
                    WHERE id = ? AND state = ? AND fragment_purchase_id IS NULL
                    """,
                    (
                        purchase_id,
                        update_text,
                        order_id,
                        OrderState.PURCHASING.value,
                    ),
                )
                return cursor.rowcount == 1

    async def finish_order(
        self,
        order_id: int,
        state: OrderState,
        *,
        final_transaction_hash: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        updated_at: datetime | None = None,
    ) -> bool:
        _require_positive_int(order_id, "order_id")
        if not isinstance(state, OrderState) or not can_transition(
            OrderState.PURCHASING, state
        ):
            raise ValueError("state must be a declared purchasing terminal state")
        _require_optional_string(final_transaction_hash, "final_transaction_hash")
        _require_optional_string(error_code, "error_code")
        _require_optional_string(error_message, "error_message")
        update_text = _datetime_text(updated_at or _utc_now(), "updated_at")

        async with self._lock:
            self._require_open()
            with _immediate_transaction(self._connection):
                row = self._connection.execute(
                    """
                    SELECT state, final_transaction_hash, error_code, error_message
                    FROM orders WHERE id = ?
                    """,
                    (order_id,),
                ).fetchone()
                if row is None:
                    return False
                current = OrderState(row["state"])
                if current is state:
                    return (
                        row["final_transaction_hash"] == final_transaction_hash
                        and row["error_code"] == error_code
                        and row["error_message"] == error_message
                    )
                if not can_transition(current, state):
                    return False
                cursor = self._connection.execute(
                    """
                    UPDATE orders
                    SET state = ?, final_transaction_hash = ?, error_code = ?,
                        error_message = ?, updated_at = ?
                    WHERE id = ? AND state = ?
                    """,
                    (
                        state.value,
                        final_transaction_hash,
                        error_code,
                        error_message,
                        update_text,
                        order_id,
                        current.value,
                    ),
                )
                return cursor.rowcount == 1

    async def expire_order(self, order_id: int, *, now: datetime) -> bool:
        _require_positive_int(order_id, "order_id")
        now_text = _datetime_text(now, "now")
        async with self._lock:
            self._require_open()
            with _immediate_transaction(self._connection):
                cursor = self._connection.execute(
                    """
                    UPDATE orders
                    SET state = ?, updated_at = ?
                    WHERE id = ? AND state = ? AND expires_at <= ?
                    """,
                    (
                        OrderState.EXPIRED.value,
                        now_text,
                        order_id,
                        OrderState.AWAITING_PAYMENT.value,
                        now_text,
                    ),
                )
                return cursor.rowcount == 1

    async def expire_due_orders(self, *, now: datetime, limit: int = 100) -> int:
        now_text = _datetime_text(now, "now")
        _validate_limit(limit)
        if limit == 0:
            return 0
        async with self._lock:
            self._require_open()
            with _immediate_transaction(self._connection):
                rows = self._connection.execute(
                    """
                    SELECT id FROM orders
                    WHERE state = ? AND expires_at <= ?
                    ORDER BY expires_at ASC, id ASC
                    LIMIT ?
                    """,
                    (OrderState.AWAITING_PAYMENT.value, now_text, limit),
                ).fetchall()
                if not rows:
                    return 0
                ids = [row["id"] for row in rows]
                placeholders = ",".join("?" for _ in ids)
                cursor = self._connection.execute(
                    f"""
                    UPDATE orders SET state = ?, updated_at = ?
                    WHERE state = ? AND expires_at <= ?
                      AND id IN ({placeholders})
                    """,
                    (
                        OrderState.EXPIRED.value,
                        now_text,
                        OrderState.AWAITING_PAYMENT.value,
                        now_text,
                        *ids,
                    ),
                )
                return cursor.rowcount

    async def list_scannable_orders(
        self,
        *,
        now: datetime,
        limit: int = 100,
    ) -> list[OrderRecord]:
        now_text = _datetime_text(now, "now")
        _validate_limit(limit)
        if limit == 0:
            return []
        async with self._lock:
            self._require_open()
            rows = self._connection.execute(
                """
                SELECT * FROM orders
                WHERE state = ? AND expires_at > ?
                ORDER BY expires_at ASC, id ASC
                LIMIT ?
                """,
                (OrderState.AWAITING_PAYMENT.value, now_text, limit),
            ).fetchall()
            return [self._order_from_row(row) for row in rows]

    async def list_purchase_orders(
        self,
        *,
        limit: int = 100,
    ) -> list[OrderRecord]:
        _validate_limit(limit)
        if limit == 0:
            return []
        async with self._lock:
            self._require_open()
            rows = self._connection.execute(
                """
                SELECT * FROM orders
                WHERE state IN (?, ?)
                ORDER BY updated_at ASC, id ASC
                LIMIT ?
                """,
                (
                    OrderState.PURCHASING.value,
                    OrderState.RECONCILIATION_REQUIRED.value,
                    limit,
                ),
            ).fetchall()
            return [self._order_from_row(row) for row in rows]

    async def get_scanner_cursor(self, stream_key: str) -> ScannerCursor | None:
        _require_nonempty(stream_key, "stream_key")
        async with self._lock:
            self._require_open()
            row = self._connection.execute(
                "SELECT * FROM scanner_state WHERE stream_key = ?",
                (stream_key,),
            ).fetchone()
            if row is None:
                return None
            return ScannerCursor(
                stream_key=row["stream_key"],
                logical_time=row["logical_time"],
                tx_hash=row["tx_hash"],
            )

    async def set_scanner_cursor(
        self,
        stream_key: str,
        logical_time: int,
        tx_hash: str,
    ) -> bool:
        _require_nonempty(stream_key, "stream_key")
        if type(logical_time) is not int or logical_time < 0:
            raise ValueError("logical_time must be a non-negative integer")
        _require_nonempty(tx_hash, "tx_hash")
        async with self._lock:
            self._require_open()
            with _immediate_transaction(self._connection):
                row = self._connection.execute(
                    """
                    SELECT logical_time, tx_hash FROM scanner_state
                    WHERE stream_key = ?
                    """,
                    (stream_key,),
                ).fetchone()
                incoming = (logical_time, tx_hash)
                if row is not None:
                    current = (row["logical_time"], row["tx_hash"])
                    if incoming < current:
                        return False
                    if incoming == current:
                        return True
                    cursor = self._connection.execute(
                        """
                        UPDATE scanner_state
                        SET logical_time = ?, tx_hash = ?
                        WHERE stream_key = ? AND logical_time = ? AND tx_hash = ?
                        """,
                        (
                            logical_time,
                            tx_hash,
                            stream_key,
                            row["logical_time"],
                            row["tx_hash"],
                        ),
                    )
                    return cursor.rowcount == 1
                self._connection.execute(
                    """
                    INSERT INTO scanner_state(stream_key, logical_time, tx_hash)
                    VALUES (?, ?, ?)
                    """,
                    (stream_key, logical_time, tx_hash),
                )
                return True

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("repository is closed")

    @staticmethod
    def _order_from_row(row: sqlite3.Row) -> OrderRecord:
        return OrderRecord(
            id=row["id"],
            user_id=row["user_id"],
            product=Product(row["product"]),
            recipient=row["recipient"],
            product_amount=row["product_amount"],
            months=row["months"],
            asset=Asset(row["asset"]),
            quoted_api_units=row["quoted_api_units"],
            customer_units=row["customer_units"],
            destination=row["destination"],
            reference=row["reference"],
            invoice_created_at=_datetime_from_text(row["invoice_created_at"]),
            created_at=_datetime_from_text(row["created_at"]),
            updated_at=_datetime_from_text(row["updated_at"]),
            expires_at=_datetime_from_text(row["expires_at"]),
            state=OrderState(row["state"]),
            idempotency_key=row["idempotency_key"],
            fragment_purchase_id=row["fragment_purchase_id"],
            payment_hash=row["payment_hash"],
            final_transaction_hash=row["final_transaction_hash"],
            error_code=row["error_code"],
            error_message=row["error_message"],
        )
