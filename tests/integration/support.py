"""Helpers for service-level integration tests (no HTTP), including concurrency tools."""

import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.principal import DEMO_PRINCIPAL
from app.models import Account, IdempotencyRecord, LedgerEntry, Transfer, TransferKind
from app.services.account_service import AccountService
from app.services.reconciliation_service import ReconciliationReport, ReconciliationService
from app.services.transfer_service import TransferCommand, TransferResult, TransferService

# Generous so that queueing behind 99 other transfers never times out; tests that are
# *about* timeouts pass their own small value.
LOCK_TIMEOUT_MS = 30_000

Factory = sessionmaker[Session]


def create_account(factory: Factory, *, funded: int = 0, name: str = "Merchant") -> uuid.UUID:
    with factory() as session:
        service = AccountService(session, lock_timeout_ms=LOCK_TIMEOUT_MS)
        account = service.create_account(name, "USD")
        if funded:
            service.fund_account(account.id, funded)
        return account.id


def transfer(
    factory: Factory,
    source_id: uuid.UUID,
    destination_id: uuid.UUID,
    amount_minor: int,
    *,
    key: str | None = None,
    scope: str = DEMO_PRINCIPAL.scope,
    lock_timeout_ms: int = LOCK_TIMEOUT_MS,
) -> TransferResult:
    with factory() as session:
        return TransferService(session, lock_timeout_ms=lock_timeout_ms).create_transfer(
            scope=scope,
            idempotency_key=key or str(uuid.uuid4()),
            command=TransferCommand(source_id, destination_id, amount_minor, "USD"),
        )


def balance(factory: Factory, account_id: uuid.UUID) -> int:
    with factory() as session:
        return session.get_one(Account, account_id).balance_minor


def merchant_transfer_count(factory: Factory) -> int:
    with factory() as session:
        stmt = (
            select(func.count()).select_from(Transfer).where(Transfer.kind == TransferKind.TRANSFER)
        )
        return session.scalar(stmt) or 0


def ledger_entry_count(factory: Factory, *, kind: TransferKind | None = None) -> int:
    with factory() as session:
        stmt = select(func.count()).select_from(LedgerEntry)
        if kind is not None:
            stmt = stmt.join(Transfer, Transfer.id == LedgerEntry.transfer_id).where(
                Transfer.kind == kind
            )
        return session.scalar(stmt) or 0


def idempotency_record(factory: Factory, key: str) -> IdempotencyRecord | None:
    with factory() as session:
        stmt = select(IdempotencyRecord).where(IdempotencyRecord.idempotency_key == key)
        return session.scalars(stmt).one_or_none()


def reconcile(factory: Factory) -> ReconciliationReport:
    with factory() as session:
        return ReconciliationService(session).run()


def run_concurrently[T](n: int, task: Callable[[int], T]) -> list[T | BaseException]:
    """Run task(0..n-1) on n threads released at the same instant by a barrier.

    The barrier maximises overlap, but no test relies on a particular interleaving: every
    assertion holds for *any* ordering, which is what makes the outcomes deterministic.
    Exceptions are returned (not raised) so callers can classify them.
    """
    barrier = threading.Barrier(n, timeout=60)

    def run(i: int) -> T | BaseException:
        barrier.wait()
        try:
            return task(i)
        except BaseException as exc:
            return exc

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(run, range(n)))


def wait_for_lock_waiters(engine: Engine, *, count: int = 1, timeout: float = 15) -> None:
    """Block until `count` other backends are waiting on a lock.

    This polls an observable condition (pg_stat_activity) instead of sleeping for a
    guessed duration, so the test proceeds as soon as the waiter really is blocked.
    """
    deadline = time.monotonic() + timeout
    query = text(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = current_database() AND wait_event_type = 'Lock' "
        "AND pid <> pg_backend_pid()"
    )
    while time.monotonic() < deadline:
        with engine.connect() as conn:
            if (conn.scalar(query) or 0) >= count:
                return
        time.sleep(0.01)
    raise AssertionError(f"Timed out waiting for {count} backend(s) blocked on a lock")


def classify(results: list[Any]) -> dict[str, list[Any]]:
    """Split concurrent outcomes into succeeded / rejected-by-code / exceptions."""
    buckets: dict[str, list[Any]] = {"succeeded": [], "errors": []}
    for result in results:
        if isinstance(result, BaseException):
            buckets["errors"].append(result)
        elif result.succeeded:
            buckets["succeeded"].append(result)
        else:
            buckets.setdefault(result.error_code, []).append(result)
    return buckets
