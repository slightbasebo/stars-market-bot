import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import time
from typing import Any, Mapping

from aiohttp import ClientError, ClientTimeout

from pytoniq import Address, Cell, WalletV4R2, WalletV5R1
from pytoniq.contract.wallets.wallet import WALLET_V4_R2_CODE
from pytoniq.contract.wallets.wallet_v5 import WALLET_V5_R1_CODE
from pytoniq_core.crypto.keys import (
    mnemonic_is_valid,
    mnemonic_to_private_key,
    private_key_to_public_key,
)
from pytoniq_core.tlb.account import StateInit

from .db import Repository, ScannerCursor
from .domain import (
    Asset,
    CANONICAL_USDT_MASTER,
    MatchResult,
    PaymentCandidate,
    match_payment,
)


@dataclass(frozen=True)
class WalletAddresses:
    v4: str
    v5r1: str


@dataclass(frozen=True)
class ScanBatch:
    candidates: tuple[PaymentCandidate, ...]
    logical_time: int | None
    tx_hash: str | None


@dataclass(frozen=True)
class ScanResult:
    seen: int = 0
    matched: int = 0
    unmatched: int = 0


class TonCenterTemporaryError(RuntimeError):
    def __init__(self, retry_after: float):
        super().__init__("TON Center is temporarily unavailable")
        self.retry_after = retry_after


def _address(value: object) -> Address:
    if not isinstance(value, str) or not value:
        raise ValueError("address must be a non-empty string")
    try:
        return Address(value)
    except Exception as exc:
        raise ValueError("address is not a valid TON address") from exc


def _raw_address(value: object) -> str:
    return _address(value).to_str(is_user_friendly=False)


def _friendly_address(address: Address) -> str:
    return address.to_str(
        is_user_friendly=True,
        is_url_safe=True,
        is_bounceable=False,
        is_test_only=False,
    )


def derive_wallet_addresses(seed: str) -> WalletAddresses:
    if not isinstance(seed, str):
        raise ValueError("seed must be a valid 24-word TON mnemonic")
    words = seed.split()
    if len(words) != 24 or not mnemonic_is_valid(words):
        raise ValueError("seed must be a valid 24-word TON mnemonic")
    _, private_key = mnemonic_to_private_key(words)
    public_key = private_key_to_public_key(private_key)

    v4_state = StateInit(
        code=WALLET_V4_R2_CODE,
        data=WalletV4R2.create_data_cell(public_key, wc=0),
    )
    v5_state = StateInit(
        code=WALLET_V5_R1_CODE,
        data=WalletV5R1.create_data_cell(
            public_key,
            wc=0,
            network_global_id=-239,
        ),
    )
    return WalletAddresses(
        v4=_friendly_address(Address((0, v4_state.serialize().hash))),
        v5r1=_friendly_address(Address((0, v5_state.serialize().hash))),
    )


def validate_owner_wallet(seed: str, configured_address: str) -> str:
    configured = _address(configured_address)
    derived = derive_wallet_addresses(seed)
    if _raw_address(configured_address) not in {
        _raw_address(derived.v4),
        _raw_address(derived.v5r1),
    }:
        raise ValueError("OWNER_WALLET_ADDRESS must be derived from OWNER_SEED_24W")
    return _friendly_address(configured)


def parse_plain_comment(body_boc: str | None) -> str | None:
    if not isinstance(body_boc, str) or not body_boc:
        return None
    try:
        body = Cell.one_from_boc(body_boc).begin_parse()
        if body.remaining_bits < 32 or body.load_uint(32) != 0:
            return None
        value = body.load_snake_string()
    except (Exception, AssertionError, UnicodeError):
        return None
    return value if value else None


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _decimal_int(value: object, name: str, *, positive: bool = False) -> int:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise ValueError(f"{name} must be a decimal string")
    parsed = int(value)
    if positive and parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _timestamp(value: object) -> datetime:
    if type(value) is not int or value < 0:
        raise ValueError("timestamp must be a non-negative integer")
    return datetime.fromtimestamp(value, timezone.utc)


def _rows(payload: Mapping[str, Any], key: str) -> list[object]:
    values = payload.get(key)
    if not isinstance(values, list):
        raise ValueError(f"{key} must be an array")
    return values


def _is_finalized(value: object) -> bool:
    return value == 2 or value == "finalized"


def _credited_despite_abort(
    description: Mapping[str, Any], inbound: Mapping[str, Any]
) -> bool:
    if description.get("aborted") is not True:
        return False
    credit_phase = description.get("credit_ph")
    if not isinstance(credit_phase, Mapping):
        return False
    credited = credit_phase.get("credit")
    value = inbound.get("value")
    return isinstance(credited, str) and credited == value


def parse_gram_transactions(
    payload: Mapping[str, Any], owner_wallet: str
) -> list[PaymentCandidate]:
    owner_raw = _raw_address(owner_wallet)
    candidates: list[PaymentCandidate] = []
    for raw_transaction in _rows(payload, "transactions"):
        transaction = _mapping(raw_transaction, "transaction")
        inbound_value = transaction.get("in_msg")
        if inbound_value is None:
            continue
        inbound = _mapping(inbound_value, "in_msg")
        destination = inbound.get("destination")
        if destination is None or _raw_address(destination) != owner_raw:
            continue
        value = inbound.get("value")
        if value is None or value == "0":
            continue
        if not _is_finalized(transaction.get("finality")):
            continue
        description = _mapping(transaction.get("description"), "description")
        content = _mapping(inbound.get("message_content"), "message_content")
        tx_hash = transaction.get("hash")
        if not isinstance(tx_hash, str) or not tx_hash:
            raise ValueError("transaction hash must be non-empty")
        candidates.append(
            PaymentCandidate(
                tx_hash=tx_hash,
                logical_time=_decimal_int(transaction.get("lt"), "lt"),
                destination=owner_wallet,
                asset=Asset.GRAM,
                units=_decimal_int(value, "value", positive=True),
                comment=parse_plain_comment(content.get("body")),
                timestamp=_timestamp(transaction.get("now")),
                finalized=True,
                aborted=(
                    description.get("aborted") is True
                    and not _credited_despite_abort(description, inbound)
                ),
                bounced=inbound.get("bounced") is True,
            )
        )
    return candidates


def parse_usdt_transfers(
    payload: Mapping[str, Any], owner_wallet: str
) -> list[PaymentCandidate]:
    owner_raw = _raw_address(owner_wallet)
    master_raw = _raw_address(CANONICAL_USDT_MASTER)
    candidates: list[PaymentCandidate] = []
    for raw_transfer in _rows(payload, "jetton_transfers"):
        transfer = _mapping(raw_transfer, "jetton_transfer")
        destination = transfer.get("destination")
        master = transfer.get("jetton_master")
        if destination is None or _raw_address(destination) != owner_raw:
            continue
        if master is None or _raw_address(master) != master_raw:
            continue
        if "finality" in transfer and not _is_finalized(transfer["finality"]):
            continue
        tx_hash = transfer.get("transaction_hash")
        if not isinstance(tx_hash, str) or not tx_hash:
            raise ValueError("transaction hash must be non-empty")
        candidates.append(
            PaymentCandidate(
                tx_hash=tx_hash,
                logical_time=_decimal_int(
                    transfer.get("transaction_lt"), "transaction_lt"
                ),
                destination=owner_wallet,
                asset=Asset.USDT,
                units=_decimal_int(transfer.get("amount"), "amount", positive=True),
                comment=parse_plain_comment(transfer.get("forward_payload")),
                timestamp=_timestamp(transfer.get("transaction_now")),
                finalized=True,
                aborted=transfer.get("transaction_aborted") is True,
                bounced=False,
                jetton_master=CANONICAL_USDT_MASTER,
            )
        )
    return candidates


class TonCenterClient:
    def __init__(
        self,
        session,
        base_url: str,
        *,
        api_key: str | None = None,
    ) -> None:
        self._session = session
        normalized = base_url.rstrip("/")
        self._base_url = (
            normalized if normalized.endswith("/api/v3") else normalized + "/api/v3"
        )
        self._headers = {"X-API-Key": api_key} if api_key else {}
        self._minimum_interval = 0.0 if api_key else 1.0
        self._request_lock = asyncio.Lock()
        self._last_request = 0.0
        self._retry_at = 0.0
        self._backoff = 0.0

    async def _get(self, path: str, params: Mapping[str, object]) -> Mapping[str, Any]:
        async with self._request_lock:
            if time.monotonic() < self._retry_at:
                raise TonCenterTemporaryError(self._retry_at - time.monotonic())
            delay = self._minimum_interval - (time.monotonic() - self._last_request)
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                async with self._session.get(
                    f"{self._base_url}{path}",
                    params=params,
                    headers=self._headers,
                    timeout=ClientTimeout(total=15),
                ) as response:
                    if response.status == 429 or response.status >= 500:
                        retry_after = response.headers.get("Retry-After", "0")
                        seconds = int(retry_after) if retry_after.isdigit() else 0
                        self._backoff = min(60, max(5, seconds, self._backoff * 2))
                        self._retry_at = time.monotonic() + self._backoff
                        raise TonCenterTemporaryError(self._backoff)
                    response.raise_for_status()
                    payload = await response.json()
                    self._backoff = 0.0
            except (ClientError, TimeoutError):
                self._backoff = min(60, max(5, self._backoff * 2))
                self._retry_at = time.monotonic() + self._backoff
                raise TonCenterTemporaryError(self._backoff) from None
            finally:
                self._last_request = time.monotonic()
        return _mapping(payload, "response")

    async def account_state(self, address: str) -> str:
        payload = await self._get(
            "/accountStates",
            {"address": address, "include_boc": "false"},
        )
        accounts = payload.get("accounts") or payload.get("account_states")
        if not isinstance(accounts, list) or len(accounts) != 1:
            raise ValueError("TON Center returned an invalid account state")
        account = _mapping(accounts[0], "account")
        status = account.get("status")
        if not isinstance(status, str) or not status:
            raise ValueError("TON Center returned an invalid account status")
        return status

    async def account_balance(self, address: str) -> int:
        payload = await self._get(
            "/accountStates",
            {"address": address, "include_boc": "false"},
        )
        accounts = payload.get("accounts") or payload.get("account_states")
        if not isinstance(accounts, list) or len(accounts) != 1:
            raise ValueError("TON Center returned an invalid account state")
        balance = _mapping(accounts[0], "account").get("balance")
        if not isinstance(balance, str) or not balance.isdigit():
            raise ValueError("TON Center returned an invalid account balance")
        return int(balance)

    async def scan_gram(
        self,
        owner_wallet: str,
        start_utime: int,
        cursor: ScannerCursor | None,
    ) -> ScanBatch:
        params: dict[str, object] = {
            "account": _raw_address(owner_wallet),
            "start_utime": start_utime,
            "sort": "asc",
            "limit": 100,
        }
        if cursor is not None:
            params["start_lt"] = cursor.logical_time
        payload = await self._get("/transactions", params)
        candidates = parse_gram_transactions(payload, owner_wallet)
        candidates = _after_cursor(candidates, cursor)
        batch = _batch(candidates)
        markers = [
            (_decimal_int(row.get("lt"), "lt"), row["hash"])
            for row in _rows(payload, "transactions")
            if _is_finalized(row.get("finality")) and isinstance(row.get("hash"), str)
        ]
        if markers:
            logical_time, tx_hash = max(markers)
            if cursor is None or (logical_time, tx_hash) > (cursor.logical_time, cursor.tx_hash):
                return ScanBatch(batch.candidates, logical_time, tx_hash)
        return batch

    async def scan_usdt(
        self,
        owner_wallet: str,
        start_utime: int,
        cursor: ScannerCursor | None,
    ) -> ScanBatch:
        params: dict[str, object] = {
            "owner_address": _raw_address(owner_wallet),
            "direction": "in",
            "jetton_master": _raw_address(CANONICAL_USDT_MASTER),
            "start_utime": start_utime,
            "sort": "asc",
            "limit": 100,
        }
        if cursor is not None:
            params["start_lt"] = cursor.logical_time
        payload = await self._get("/jetton/transfers", params)
        candidates = parse_usdt_transfers(payload, owner_wallet)
        candidates = _after_cursor(candidates, cursor)
        return _batch(candidates)


def _after_cursor(
    candidates: list[PaymentCandidate], cursor: ScannerCursor | None
) -> list[PaymentCandidate]:
    if cursor is None:
        return candidates
    return [
        candidate
        for candidate in candidates
        if (candidate.logical_time, candidate.tx_hash)
        > (cursor.logical_time, cursor.tx_hash)
    ]


def _batch(candidates: list[PaymentCandidate]) -> ScanBatch:
    ordered = sorted(candidates, key=lambda item: (item.logical_time, item.tx_hash))
    if not ordered:
        return ScanBatch((), None, None)
    last = ordered[-1]
    return ScanBatch(tuple(ordered), last.logical_time, last.tx_hash)


class PaymentScanner:
    def __init__(
        self,
        repo: Repository,
        client: TonCenterClient,
        owner_wallet: str,
    ) -> None:
        self._repo = repo
        self._client = client
        self._owner_wallet = owner_wallet
        self._scan_lock = asyncio.Lock()

    async def scan_once(self, *, now: datetime | None = None) -> ScanResult:
        async with self._scan_lock:
            return await self._scan_once()

    async def _scan_once(self) -> ScanResult:
        starts = await self._repo.payment_scan_starts()
        if not starts:
            return ScanResult()
        batches: list[tuple[str, ScanBatch]] = []
        if Asset.GRAM in starts:
            cursor = await self._repo.get_scanner_cursor("gram")
            batches.append(
                (
                    "gram",
                    await self._client.scan_gram(
                        self._owner_wallet, int(starts[Asset.GRAM].timestamp()), cursor
                    ),
                )
            )
        if Asset.USDT in starts:
            cursor = await self._repo.get_scanner_cursor("usdt")
            batches.append(
                (
                    "usdt",
                    await self._client.scan_usdt(
                        self._owner_wallet, int(starts[Asset.USDT].timestamp()), cursor
                    ),
                )
            )

        seen = matched = unmatched = 0
        for stream_key, batch in batches:
            for candidate in batch.candidates:
                if await self._repo.has_payment(candidate.tx_hash):
                    continue
                seen += 1
                order = None
                if candidate.comment:
                    order = await self._repo.find_invoice_by_reference(
                        candidate.comment
                    )
                result = MatchResult.WRONG_REFERENCE
                if order is not None and order.invoice is not None:
                    result = match_payment(order.invoice, candidate)
                recorded = await self._repo.record_payment(
                    order.id if order is not None else None,
                    candidate,
                    result,
                )
                if result is MatchResult.MATCH and recorded:
                    matched += 1
                else:
                    unmatched += 1
            if batch.logical_time is not None and batch.tx_hash is not None:
                await self._repo.set_scanner_cursor(
                    stream_key,
                    batch.logical_time,
                    batch.tx_hash,
                )
        return ScanResult(seen=seen, matched=matched, unmatched=unmatched)
