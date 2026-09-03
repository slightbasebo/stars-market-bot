import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import sqlite3
import threading

import pytest

from stars_market_bot.db import Repository
from stars_market_bot.domain import (
    Asset,
    Invoice,
    MatchResult,
    OrderState,
    PaymentCandidate,
    Product,
)


NOW = datetime(2026, 9, 3, 12, 0, 0, 123456, tzinfo=timezone.utc)


class FailNextCommitConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._should_fail = True

    def commit(self) -> None:
        if self._should_fail:
            self._should_fail = False
            raise RuntimeError("simulated commit failure")
        self._connection.commit()

    def __getattr__(self, name):
        return getattr(self._connection, name)


class PauseCommitConnection:
    def __init__(
        self,
        connection: sqlite3.Connection,
        write_locked: threading.Event,
        release_write: threading.Event,
    ) -> None:
        self._connection = connection
        self._write_locked = write_locked
        self._release_write = release_write

    def commit(self) -> None:
        self._write_locked.set()
        if not self._release_write.wait(timeout=2):
            raise TimeoutError("test did not release SQLite write lock")
        self._connection.commit()

    def __getattr__(self, name):
        return getattr(self._connection, name)


class ObserveBeginConnection:
    def __init__(
        self,
        connection: sqlite3.Connection,
        begin_attempted: threading.Event,
    ) -> None:
        self._connection = connection
        self._begin_attempted = begin_attempted

    def execute(self, sql, parameters=()):
        if sql.strip().upper() == "BEGIN IMMEDIATE":
            self._begin_attempted.set()
        return self._connection.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self._connection, name)


def run_two_connection_write_race(database_path, first_operation, second_operation):
    write_locked = threading.Event()
    release_write = threading.Event()
    begin_attempted = threading.Event()
    connections_ready = threading.Barrier(2)

    def run_first():
        async def scenario():
            repo = await Repository.open(database_path)
            repo._connection = PauseCommitConnection(
                repo._connection,
                write_locked,
                release_write,
            )
            try:
                connections_ready.wait(timeout=2)
                return await first_operation(repo)
            finally:
                await repo.close()

        return asyncio.run(scenario())

    def run_second():
        async def scenario():
            repo = await Repository.open(database_path)
            repo._connection = ObserveBeginConnection(
                repo._connection,
                begin_attempted,
            )
            try:
                connections_ready.wait(timeout=2)
                if not write_locked.wait(timeout=2):
                    raise TimeoutError("first connection did not acquire write lock")
                return await second_operation(repo)
            finally:
                await repo.close()

        return asyncio.run(scenario())

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(run_first)
        second_future = executor.submit(run_second)
        try:
            assert write_locked.wait(timeout=2)
            assert begin_attempted.wait(timeout=2)
            assert not second_future.done()
        finally:
            release_write.set()
        return first_future.result(timeout=2), second_future.result(timeout=2)


def invoice_fixture(
    *,
    asset: Asset = Asset.GRAM,
    units: int = 2_000_000_000,
    destination: str = "UQ-store-wallet",
    reference: str = "SM-7K4Q2P",
    created_at: datetime = NOW + timedelta(minutes=1),
    expires_at: datetime = NOW + timedelta(minutes=16),
) -> Invoice:
    return Invoice(
        destination=destination,
        asset=asset,
        units=units,
        reference=reference,
        created_at=created_at,
        expires_at=expires_at,
    )


def payment_fixture(
    *,
    tx_hash: str = "A1",
    logical_time: int = 41,
    asset: Asset = Asset.GRAM,
    units: int = 2_000_000_000,
    destination: str = "UQ-store-wallet",
    comment: str | None = "SM-7K4Q2P",
    timestamp: datetime = NOW + timedelta(minutes=2),
    jetton_master: str | None = None,
) -> PaymentCandidate:
    return PaymentCandidate(
        tx_hash=tx_hash,
        logical_time=logical_time,
        destination=destination,
        asset=asset,
        units=units,
        comment=comment,
        timestamp=timestamp,
        finalized=True,
        aborted=False,
        bounced=False,
        jetton_master=jetton_master,
    )


async def create_draft(
    repo: Repository,
    *,
    user_id: int = 1001,
    product: Product = Product.STARS,
    recipient: str = "alice_1",
    product_amount: int | None = 250,
    months: int | None = None,
    asset: Asset = Asset.GRAM,
    quoted_api_units: int = 1_800_000_000,
    idempotency_key: str = "order-key-1",
    created_at: datetime = NOW,
):
    return await repo.create_order(
        user_id=user_id,
        product=product,
        recipient=recipient,
        product_amount=product_amount,
        months=months,
        asset=asset,
        quoted_api_units=quoted_api_units,
        idempotency_key=idempotency_key,
        created_at=created_at,
    )


async def create_awaiting_order(
    repo: Repository,
    *,
    user_id: int = 1001,
    idempotency_key: str = "order-key-1",
    reference: str = "SM-7K4Q2P",
    created_at: datetime = NOW,
    invoice_created_at: datetime | None = None,
    expires_at: datetime | None = None,
):
    order = await create_draft(
        repo,
        user_id=user_id,
        idempotency_key=idempotency_key,
        created_at=created_at,
    )
    invoice = invoice_fixture(
        reference=reference,
        created_at=invoice_created_at or created_at + timedelta(minutes=1),
        expires_at=expires_at or created_at + timedelta(minutes=16),
    )
    assert await repo.set_invoice(
        order.id,
        invoice,
        updated_at=invoice.created_at,
    )
    result = await repo.get_order(order.id)
    assert result is not None
    return result


async def create_paid_order(
    repo: Repository,
    *,
    user_id: int = 1001,
    idempotency_key: str = "order-key-1",
    reference: str = "SM-7K4Q2P",
    tx_hash: str = "A1",
    created_at: datetime = NOW,
):
    order = await create_awaiting_order(
        repo,
        user_id=user_id,
        idempotency_key=idempotency_key,
        reference=reference,
        created_at=created_at,
    )
    payment = payment_fixture(
        tx_hash=tx_hash,
        comment=reference,
        timestamp=created_at + timedelta(minutes=2),
    )
    assert await repo.record_payment(order.id, payment, MatchResult.MATCH)
    result = await repo.get_order(order.id)
    assert result is not None
    return result


def test_setup_enables_wal_and_foreign_keys_and_is_idempotent(tmp_path):
    async def scenario():
        repo = await Repository.open(tmp_path / "bot.sqlite3")
        try:
            await repo.setup()
            await repo.setup()

            journal_mode = repo._connection.execute("PRAGMA journal_mode").fetchone()[0]
            foreign_keys = repo._connection.execute("PRAGMA foreign_keys").fetchone()[0]
            busy_timeout = repo._connection.execute("PRAGMA busy_timeout").fetchone()[0]

            assert journal_mode == "wal"
            assert foreign_keys == 1
            assert busy_timeout > 0
        finally:
            await repo.close()

    asyncio.run(scenario())


def test_language_upsert_round_trip_and_supported_language_validation(tmp_path):
    async def scenario():
        repo = await Repository.open(tmp_path / "bot.sqlite3")
        try:
            await repo.setup()
            assert await repo.get_language(8670053970) is None

            await repo.set_language(8670053970, "ru")
            assert await repo.get_language(8670053970) == "ru"

            await repo.set_language(8670053970, "tr")
            assert await repo.get_language(8670053970) == "tr"

            with pytest.raises(ValueError, match="language"):
                await repo.set_language(8670053970, "de")
        finally:
            await repo.close()

    asyncio.run(scenario())


def test_complete_order_and_invoice_round_trip_losslessly(tmp_path):
    async def scenario():
        repo = await Repository.open(tmp_path / "bot.sqlite3")
        try:
            await repo.setup()
            order = await create_draft(
                repo,
                user_id=8670053970,
                product=Product.PREMIUM,
                recipient="recipient_7",
                product_amount=None,
                months=12,
                asset=Asset.USDT,
                quoted_api_units=9_876_543,
                idempotency_key="premium-order-7",
                created_at=NOW,
            )
            invoice = invoice_fixture(
                asset=Asset.USDT,
                units=10_864_198,
                destination="UQ-usdt-wallet",
                reference="SM-PREMIUM7",
                created_at=NOW + timedelta(seconds=1, microseconds=1),
                expires_at=NOW + timedelta(minutes=15, seconds=1, microseconds=1),
            )

            assert await repo.set_invoice(
                order.id,
                invoice,
                updated_at=invoice.created_at,
            )
            loaded = await repo.get_order(order.id, user_id=8670053970)

            assert loaded is not None
            assert loaded.user_id == 8670053970
            assert loaded.product is Product.PREMIUM
            assert loaded.recipient == "recipient_7"
            assert loaded.product_amount is None
            assert loaded.months == 12
            assert loaded.asset is Asset.USDT
            assert loaded.quoted_api_units == 9_876_543
            assert loaded.customer_units == 10_864_198
            assert loaded.destination == "UQ-usdt-wallet"
            assert loaded.reference == "SM-PREMIUM7"
            assert loaded.created_at == NOW
            assert loaded.updated_at == invoice.created_at
            assert loaded.expires_at == invoice.expires_at
            assert loaded.state is OrderState.AWAITING_PAYMENT
            assert loaded.idempotency_key == "premium-order-7"
            assert loaded.fragment_purchase_id is None
            assert loaded.payment_hash is None
            assert loaded.final_transaction_hash is None
            assert loaded.error_code is None
            assert loaded.error_message is None
            assert loaded.invoice == invoice
            assert await repo.get_order(order.id, user_id=123) is None
        finally:
            await repo.close()

    asyncio.run(scenario())


def test_repository_rejects_naive_timestamps_at_boundary(tmp_path):
    async def scenario():
        repo = await Repository.open(tmp_path / "bot.sqlite3")
        try:
            await repo.setup()
            with pytest.raises(ValueError, match="UTC"):
                await create_draft(
                    repo,
                    created_at=datetime(2026, 9, 3, 12, 0),
                )
        finally:
            await repo.close()

    asyncio.run(scenario())


def test_duplicate_idempotency_key_is_rejected(tmp_path):
    async def scenario():
        repo = await Repository.open(tmp_path / "bot.sqlite3")
        try:
            await repo.setup()
            await create_draft(repo)

            with pytest.raises(sqlite3.IntegrityError):
                await create_draft(repo, user_id=1002)
        finally:
            await repo.close()

    asyncio.run(scenario())


def test_duplicate_reference_is_rejected_and_transaction_recovers(tmp_path):
    async def scenario():
        repo = await Repository.open(tmp_path / "bot.sqlite3")
        try:
            await repo.setup()
            first = await create_draft(repo, idempotency_key="one")
            second = await create_draft(repo, user_id=1002, idempotency_key="two")
            invoice = invoice_fixture(reference="SM-SHARED")
            assert await repo.set_invoice(first.id, invoice)

            with pytest.raises(sqlite3.IntegrityError):
                await repo.set_invoice(second.id, invoice)

            replacement = replace(invoice, reference="SM-RECOVERED")
            assert await repo.set_invoice(second.id, replacement)
            assert (await repo.get_order(second.id)).reference == "SM-RECOVERED"
        finally:
            await repo.close()

    asyncio.run(scenario())


def test_commit_failure_rolls_back_and_connection_remains_usable(tmp_path):
    async def scenario():
        repo = await Repository.open(tmp_path / "bot.sqlite3")
        try:
            await repo.setup()
            repo._connection = FailNextCommitConnection(repo._connection)

            with pytest.raises(RuntimeError, match="simulated commit failure"):
                await create_draft(repo, idempotency_key="rolled-back")

            assert not repo._connection.in_transaction
            saved = await create_draft(repo, idempotency_key="after-failure")
            assert (await repo.get_order(saved.id)).idempotency_key == "after-failure"
        finally:
            await repo.close()

    asyncio.run(scenario())


def test_set_invoice_is_compare_and_set_and_does_not_overwrite(tmp_path):
    async def scenario():
        repo = await Repository.open(tmp_path / "bot.sqlite3")
        try:
            await repo.setup()
            order = await create_draft(repo)
            first = invoice_fixture()
            second = replace(first, reference="SM-OTHER")

            assert await repo.set_invoice(order.id, first)
            assert not await repo.set_invoice(order.id, second)
            assert (await repo.get_order(order.id)).invoice == first
        finally:
            await repo.close()

    asyncio.run(scenario())


def test_payment_hash_can_be_consumed_once(tmp_path):
    async def scenario():
        repo = await Repository.open(tmp_path / "bot.sqlite3")
        try:
            await repo.setup()
            order = await create_awaiting_order(repo)

            assert await repo.record_payment(
                order.id,
                payment_fixture(tx_hash="A1"),
            )
            assert not await repo.record_payment(
                order.id,
                payment_fixture(tx_hash="A1"),
            )
            assert (await repo.get_order(order.id)).payment_hash == "A1"
        finally:
            await repo.close()

    asyncio.run(scenario())


def test_same_payment_racing_across_two_orders_credits_only_one(tmp_path):
    async def scenario():
        repo = await Repository.open(tmp_path / "bot.sqlite3")
        try:
            await repo.setup()
            first = await create_awaiting_order(
                repo,
                idempotency_key="one",
                reference="SM-ONE",
            )
            second = await create_awaiting_order(
                repo,
                user_id=1002,
                idempotency_key="two",
                reference="SM-TWO",
            )
            candidate = payment_fixture(tx_hash="shared", comment="SM-ONE")

            results = await asyncio.gather(
                repo.record_payment(first.id, candidate, MatchResult.MATCH),
                repo.record_payment(second.id, candidate, MatchResult.MATCH),
            )
            loaded = [await repo.get_order(first.id), await repo.get_order(second.id)]

            assert sum(results) == 1
            assert sum(order.state is OrderState.PAID for order in loaded) == 1
            assert sum(order.payment_hash == "shared" for order in loaded) == 1
        finally:
            await repo.close()

    asyncio.run(scenario())


def test_same_payment_race_across_connections_and_orders_credits_once(tmp_path):
    database_path = tmp_path / "bot.sqlite3"

    async def prepare():
        repo = await Repository.open(database_path)
        try:
            await repo.setup()
            first = await create_awaiting_order(
                repo,
                idempotency_key="connection-one",
                reference="SM-CONNECTION-ONE",
            )
            second = await create_awaiting_order(
                repo,
                user_id=1002,
                idempotency_key="connection-two",
                reference="SM-CONNECTION-TWO",
            )
            return first, second
        finally:
            await repo.close()

    first_order, second_order = asyncio.run(prepare())
    candidate = payment_fixture(
        tx_hash="two-connection-payment",
        comment="SM-CONNECTION-ONE",
    )

    async def record_first(repo):
        return await repo.record_payment(
            first_order.id,
            candidate,
            MatchResult.MATCH,
        )

    async def record_second(repo):
        return await repo.record_payment(
            second_order.id,
            candidate,
            MatchResult.MATCH,
        )

    first, second = run_two_connection_write_race(
        database_path,
        record_first,
        record_second,
    )

    async def verify():
        repo = await Repository.open(database_path)
        try:
            loaded_first = await repo.get_order(first_order.id)
            loaded_second = await repo.get_order(second_order.id)
            rows = repo._connection.execute(
                "SELECT tx_hash, order_id, credited FROM payments"
            ).fetchall()
            assert (first, second) == (True, False)
            assert loaded_first.state is OrderState.PAID
            assert loaded_first.payment_hash == candidate.tx_hash
            assert loaded_second.state is OrderState.AWAITING_PAYMENT
            assert loaded_second.payment_hash is None
            assert [tuple(row) for row in rows] == [
                (candidate.tx_hash, first_order.id, 1)
            ]
        finally:
            await repo.close()

    asyncio.run(verify())


def test_two_matching_payments_racing_for_one_order_credit_once_and_both_are_audited(
    tmp_path,
):
    database_path = tmp_path / "bot.sqlite3"

    async def scenario():
        repo = await Repository.open(database_path)
        try:
            await repo.setup()
            order = await create_awaiting_order(repo)
            first = payment_fixture(tx_hash="first")
            second = payment_fixture(tx_hash="second", logical_time=42)

            results = await asyncio.gather(
                repo.record_payment(order.id, first, MatchResult.MATCH),
                repo.record_payment(order.id, second, MatchResult.MATCH),
            )
            loaded = await repo.get_order(order.id)

            assert sum(results) == 1
            assert loaded.state is OrderState.PAID
            assert loaded.payment_hash in {"first", "second"}
        finally:
            await repo.close()

    asyncio.run(scenario())

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT tx_hash, credited FROM payments ORDER BY tx_hash"
        ).fetchall()
    assert rows == [("first", 0), ("second", 1)] or rows == [
        ("first", 1),
        ("second", 0),
    ]


def test_unmatched_payment_is_audited_without_changing_order_state(tmp_path):
    database_path = tmp_path / "bot.sqlite3"

    async def scenario():
        repo = await Repository.open(database_path)
        try:
            await repo.setup()
            order = await create_awaiting_order(repo)
            candidate = payment_fixture(tx_hash="wrong", units=1)

            assert await repo.record_payment(
                order.id,
                candidate,
                MatchResult.WRONG_AMOUNT,
            )
            assert (await repo.get_order(order.id)).state is OrderState.AWAITING_PAYMENT
        finally:
            await repo.close()

    asyncio.run(scenario())

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT match_result, credited FROM payments WHERE tx_hash = 'wrong'"
        ).fetchone()
    assert row == ("wrong_amount", 0)


def test_claim_paid_order_is_compare_and_set(tmp_path):
    async def scenario():
        repo = await Repository.open(tmp_path / "bot.sqlite3")
        try:
            await repo.setup()
            await create_paid_order(repo)

            first, second = await asyncio.gather(
                repo.claim_paid_order(updated_at=NOW + timedelta(minutes=3)),
                repo.claim_paid_order(updated_at=NOW + timedelta(minutes=3)),
            )

            assert sum(value is not None for value in (first, second)) == 1
            claimed = first or second
            assert claimed.state is OrderState.PURCHASING
        finally:
            await repo.close()

    asyncio.run(scenario())


def test_claim_paid_order_race_across_connections_waits_and_claims_once(tmp_path):
    database_path = tmp_path / "bot.sqlite3"

    async def prepare():
        repo = await Repository.open(database_path)
        try:
            await repo.setup()
            return await create_paid_order(repo)
        finally:
            await repo.close()

    paid = asyncio.run(prepare())

    async def claim(repo):
        return await repo.claim_paid_order(updated_at=NOW + timedelta(minutes=3))

    first, second = run_two_connection_write_race(
        database_path,
        claim,
        claim,
    )

    async def verify():
        repo = await Repository.open(database_path)
        try:
            loaded = await repo.get_order(paid.id)
            assert first is not None
            assert first.id == paid.id
            assert second is None
            assert loaded.state is OrderState.PURCHASING
        finally:
            await repo.close()

    asyncio.run(verify())


def test_claim_paid_order_uses_deterministic_oldest_first_order(tmp_path):
    async def scenario():
        repo = await Repository.open(tmp_path / "bot.sqlite3")
        try:
            await repo.setup()
            older = await create_paid_order(
                repo,
                user_id=1001,
                idempotency_key="older",
                reference="SM-OLDER",
                tx_hash="older-payment",
                created_at=NOW,
            )
            await create_paid_order(
                repo,
                user_id=1002,
                idempotency_key="newer",
                reference="SM-NEWER",
                tx_hash="newer-payment",
                created_at=NOW + timedelta(seconds=1),
            )

            claimed = await repo.claim_paid_order()

            assert claimed.id == older.id
        finally:
            await repo.close()

    asyncio.run(scenario())


def test_fragment_purchase_id_is_idempotent_but_conflicts_fail_closed(tmp_path):
    async def scenario():
        repo = await Repository.open(tmp_path / "bot.sqlite3")
        try:
            await repo.setup()
            paid = await create_paid_order(repo)
            claimed = await repo.claim_paid_order()
            assert claimed.id == paid.id

            assert await repo.record_fragment_purchase(claimed.id, "purchase-9")
            assert await repo.record_fragment_purchase(claimed.id, "purchase-9")
            assert not await repo.record_fragment_purchase(claimed.id, "purchase-other")
            assert (await repo.get_order(claimed.id)).fragment_purchase_id == "purchase-9"
        finally:
            await repo.close()

    asyncio.run(scenario())


def test_duplicate_fragment_purchase_id_is_rejected_and_connection_recovers(tmp_path):
    async def scenario():
        repo = await Repository.open(tmp_path / "bot.sqlite3")
        try:
            await repo.setup()
            await create_paid_order(
                repo,
                idempotency_key="one",
                reference="SM-ONE",
                tx_hash="pay-one",
            )
            await create_paid_order(
                repo,
                user_id=1002,
                idempotency_key="two",
                reference="SM-TWO",
                tx_hash="pay-two",
                created_at=NOW + timedelta(seconds=1),
            )
            first = await repo.claim_paid_order()
            second = await repo.claim_paid_order()
            assert first is not None and second is not None
            assert await repo.record_fragment_purchase(first.id, "purchase-shared")

            with pytest.raises(sqlite3.IntegrityError):
                await repo.record_fragment_purchase(second.id, "purchase-shared")

            assert await repo.record_fragment_purchase(second.id, "purchase-distinct")
        finally:
            await repo.close()

    asyncio.run(scenario())


def test_finish_order_allows_declared_terminal_transition_and_exact_retry(tmp_path):
    async def scenario():
        repo = await Repository.open(tmp_path / "bot.sqlite3")
        try:
            await repo.setup()
            await create_paid_order(repo)
            claimed = await repo.claim_paid_order()
            finished_at = NOW + timedelta(minutes=4)

            assert await repo.finish_order(
                claimed.id,
                OrderState.COMPLETED,
                final_transaction_hash="fragment-tx-7",
                updated_at=finished_at,
            )
            assert await repo.finish_order(
                claimed.id,
                OrderState.COMPLETED,
                final_transaction_hash="fragment-tx-7",
                updated_at=finished_at,
            )
            loaded = await repo.get_order(claimed.id)
            assert loaded.state is OrderState.COMPLETED
            assert loaded.final_transaction_hash == "fragment-tx-7"
        finally:
            await repo.close()

    asyncio.run(scenario())


def test_paid_failed_order_can_be_requeued_with_fresh_purchase_identity(tmp_path):
    async def scenario():
        repo = await Repository.open(tmp_path / "bot.sqlite3")
        try:
            await repo.setup()
            await create_paid_order(repo)
            claimed = await repo.claim_paid_order()
            assert await repo.record_fragment_purchase(claimed.id, "failed-purchase")
            assert await repo.finish_order(
                claimed.id,
                OrderState.MANUAL_REVIEW,
                error_code="TOP_UP_REQUIRED",
                error_message="Owner wallet requires a GRAM top-up",
            )

            assert await repo.retry_paid_order(claimed.id, "fresh-key")
            retried = await repo.get_order(claimed.id)
            assert retried.state is OrderState.PAID
            assert retried.idempotency_key == "fresh-key"
            assert retried.fragment_purchase_id is None
            assert retried.error_code is None
        finally:
            await repo.close()

    asyncio.run(scenario())


def test_reconciliation_can_settle_but_completed_order_stays_terminal(tmp_path):
    async def scenario():
        repo = await Repository.open(tmp_path / "bot.sqlite3")
        try:
            await repo.setup()
            paid = await create_paid_order(repo)

            assert not await repo.finish_order(paid.id, OrderState.FAILED)
            with pytest.raises(ValueError, match="terminal"):
                await repo.finish_order(paid.id, OrderState.EXPIRED)

            claimed = await repo.claim_paid_order()
            assert await repo.finish_order(
                claimed.id,
                OrderState.RECONCILIATION_REQUIRED,
                error_code="unknown-status",
                error_message="Safe public detail",
            )
            assert await repo.finish_order(
                claimed.id,
                OrderState.COMPLETED,
                final_transaction_hash="settled-tx",
            )
            assert not await repo.record_fragment_purchase(claimed.id, "purchase-late")
            assert not await repo.finish_order(
                claimed.id,
                OrderState.FAILED,
                error_code="replacement",
            )
            loaded = await repo.get_order(claimed.id)
            assert loaded.state is OrderState.COMPLETED
            assert loaded.final_transaction_hash == "settled-tx"
            assert loaded.error_code is None
            assert loaded.error_message is None
        finally:
            await repo.close()

    asyncio.run(scenario())


def test_expire_order_rejects_early_expiry_and_is_terminal_at_deadline(tmp_path):
    async def scenario():
        repo = await Repository.open(tmp_path / "bot.sqlite3")
        try:
            await repo.setup()
            order = await create_awaiting_order(
                repo,
                expires_at=NOW + timedelta(minutes=5),
            )

            assert not await repo.expire_order(
                order.id,
                now=NOW + timedelta(minutes=5) - timedelta(microseconds=1),
            )
            assert (await repo.get_order(order.id)).state is OrderState.AWAITING_PAYMENT

            assert await repo.expire_order(
                order.id,
                now=NOW + timedelta(minutes=5),
            )
            assert not await repo.expire_order(
                order.id,
                now=NOW + timedelta(minutes=6),
            )
            assert (await repo.get_order(order.id)).state is OrderState.EXPIRED
        finally:
            await repo.close()

    asyncio.run(scenario())


def test_expire_order_never_changes_paid_order(tmp_path):
    async def scenario():
        repo = await Repository.open(tmp_path / "bot.sqlite3")
        try:
            await repo.setup()
            order = await create_paid_order(repo)

            assert not await repo.expire_order(
                order.id,
                now=NOW + timedelta(hours=1),
            )
            loaded = await repo.get_order(order.id)
            assert loaded.state is OrderState.PAID
            assert loaded.payment_hash == "A1"
        finally:
            await repo.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("payment_first", [False, True])
def test_expiry_and_payment_race_has_one_durable_winner(tmp_path, payment_first):
    database_path = tmp_path / "bot.sqlite3"

    async def prepare():
        repo = await Repository.open(database_path)
        try:
            await repo.setup()
            return await create_awaiting_order(
                repo,
                expires_at=NOW + timedelta(minutes=5),
            )
        finally:
            await repo.close()

    order = asyncio.run(prepare())
    candidate = payment_fixture(
        tx_hash="expiry-race-payment",
        timestamp=NOW + timedelta(minutes=4),
    )

    async def expire(repo):
        return await repo.expire_order(
            order.id,
            now=NOW + timedelta(minutes=5),
        )

    async def pay(repo):
        return await repo.record_payment(
            order.id,
            candidate,
            MatchResult.MATCH,
        )

    first, second = run_two_connection_write_race(
        database_path,
        pay if payment_first else expire,
        expire if payment_first else pay,
    )
    paid, expired = (first, second) if payment_first else (second, first)

    async def verify():
        repo = await Repository.open(database_path)
        try:
            loaded = await repo.get_order(order.id)
            payment_row = repo._connection.execute(
                "SELECT credited FROM payments WHERE tx_hash = ?",
                (candidate.tx_hash,),
            ).fetchone()
            assert expired is not paid
            assert loaded.state is (
                OrderState.EXPIRED if expired else OrderState.PAID
            )
            assert loaded.payment_hash == (None if expired else candidate.tx_hash)
            assert payment_row["credited"] == int(paid)
        finally:
            await repo.close()

    asyncio.run(verify())


def test_list_user_orders_is_scoped_limited_and_deterministic(tmp_path):
    async def scenario():
        repo = await Repository.open(tmp_path / "bot.sqlite3")
        try:
            await repo.setup()
            first = await create_draft(
                repo,
                idempotency_key="first",
                created_at=NOW,
            )
            second = await create_draft(
                repo,
                idempotency_key="second",
                created_at=NOW + timedelta(seconds=1),
            )
            await create_draft(
                repo,
                user_id=9999,
                idempotency_key="other-user",
                created_at=NOW + timedelta(seconds=2),
            )

            orders = await repo.list_user_orders(1001, limit=2)

            assert [order.id for order in orders] == [second.id, first.id]
            assert await repo.list_user_orders(1001, limit=0) == []
        finally:
            await repo.close()

    asyncio.run(scenario())


def test_scanner_listing_filters_expired_and_non_awaiting_orders(tmp_path):
    async def scenario():
        repo = await Repository.open(tmp_path / "bot.sqlite3")
        try:
            await repo.setup()
            later = await create_awaiting_order(
                repo,
                idempotency_key="later",
                reference="SM-LATER",
                expires_at=NOW + timedelta(minutes=10),
            )
            sooner = await create_awaiting_order(
                repo,
                idempotency_key="sooner",
                reference="SM-SOONER",
                invoice_created_at=NOW - timedelta(minutes=5),
                expires_at=NOW + timedelta(minutes=5),
            )
            await create_awaiting_order(
                repo,
                idempotency_key="expired",
                reference="SM-EXPIRED",
                invoice_created_at=NOW - timedelta(minutes=20),
                expires_at=NOW - timedelta(minutes=1),
            )
            paid = await create_awaiting_order(
                repo,
                idempotency_key="paid",
                reference="SM-PAID",
            )
            assert await repo.record_payment(
                paid.id,
                payment_fixture(tx_hash="paid", comment="SM-PAID"),
                MatchResult.MATCH,
            )

            orders = await repo.list_scannable_orders(now=NOW, limit=20)

            assert [order.id for order in orders] == [sooner.id, later.id]
        finally:
            await repo.close()

    asyncio.run(scenario())


def test_purchase_listing_includes_saved_ids_for_recovery(tmp_path):
    async def scenario():
        repo = await Repository.open(tmp_path / "bot.sqlite3")
        try:
            await repo.setup()
            await create_paid_order(
                repo,
                idempotency_key="one",
                reference="SM-ONE",
                tx_hash="pay-one",
            )
            await create_paid_order(
                repo,
                user_id=1002,
                idempotency_key="two",
                reference="SM-TWO",
                tx_hash="pay-two",
                created_at=NOW + timedelta(seconds=1),
            )
            first = await repo.claim_paid_order(updated_at=NOW + timedelta(minutes=3))
            second = await repo.claim_paid_order(updated_at=NOW + timedelta(minutes=4))
            assert await repo.record_fragment_purchase(
                second.id,
                "purchase-2",
                updated_at=NOW + timedelta(minutes=5),
            )
            assert await repo.finish_order(
                second.id,
                OrderState.RECONCILIATION_REQUIRED,
                error_code="RECONCILIATION_REQUIRED",
                error_message="Awaiting final API status",
                updated_at=NOW + timedelta(minutes=6),
            )

            orders = await repo.list_purchase_orders(limit=20)

            assert [order.id for order in orders] == [first.id, second.id]
            assert [order.fragment_purchase_id for order in orders] == [
                None,
                "purchase-2",
            ]
        finally:
            await repo.close()

    asyncio.run(scenario())


def test_find_invoice_by_reference_returns_exact_order(tmp_path):
    async def scenario():
        repo = await Repository.open(tmp_path / "bot.sqlite3")
        try:
            await repo.setup()
            order = await create_awaiting_order(repo, reference="SM-FIND-ME")

            assert (await repo.find_invoice_by_reference("SM-FIND-ME")).id == order.id
            assert await repo.find_invoice_by_reference("SM-MISSING") is None
        finally:
            await repo.close()

    asyncio.run(scenario())


def test_cursor_is_monotonic_and_persists_across_reopen(tmp_path):
    database_path = tmp_path / "bot.sqlite3"

    async def first_session():
        repo = await Repository.open(database_path)
        await repo.setup()
        assert await repo.get_scanner_cursor("gram") is None
        assert await repo.set_scanner_cursor("gram", 41, "hash-b")
        assert await repo.set_scanner_cursor("gram", 41, "hash-c")
        assert not await repo.set_scanner_cursor("gram", 41, "hash-a")
        assert not await repo.set_scanner_cursor("gram", 40, "hash-z")
        assert await repo.set_scanner_cursor("gram", 41, "hash-c")
        await repo.close()

    async def second_session():
        repo = await Repository.open(database_path)
        try:
            await repo.setup()
            cursor = await repo.get_scanner_cursor("gram")
            assert cursor.stream_key == "gram"
            assert cursor.logical_time == 41
            assert cursor.tx_hash == "hash-c"
        finally:
            await repo.close()

    asyncio.run(first_session())
    asyncio.run(second_session())


def test_cursor_progression_across_connections_waits_and_remains_monotonic(tmp_path):
    database_path = tmp_path / "bot.sqlite3"

    async def prepare():
        repo = await Repository.open(database_path)
        try:
            await repo.setup()
        finally:
            await repo.close()

    asyncio.run(prepare())

    async def set_earlier(repo):
        return await repo.set_scanner_cursor("gram", 41, "hash-b")

    async def set_later(repo):
        return await repo.set_scanner_cursor("gram", 42, "hash-a")

    first, second = run_two_connection_write_race(
        database_path,
        set_earlier,
        set_later,
    )

    async def verify():
        repo = await Repository.open(database_path)
        try:
            cursor = await repo.get_scanner_cursor("gram")
            assert (first, second) == (True, True)
            assert cursor.logical_time == 42
            assert cursor.tx_hash == "hash-a"
            assert not await repo.set_scanner_cursor("gram", 41, "hash-z")
            persisted = await repo.get_scanner_cursor("gram")
            assert persisted == cursor
        finally:
            await repo.close()

    asyncio.run(verify())


@pytest.mark.parametrize("limit", [-1, True, 1.5])
def test_listing_rejects_invalid_limits(tmp_path, limit):
    async def scenario():
        repo = await Repository.open(tmp_path / "bot.sqlite3")
        try:
            await repo.setup()
            with pytest.raises(ValueError, match="limit"):
                await repo.list_user_orders(1001, limit=limit)
        finally:
            await repo.close()

    asyncio.run(scenario())
