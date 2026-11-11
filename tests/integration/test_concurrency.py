"""Concurrent transfers against real PostgreSQL row locks.

Every thread uses its own session (and therefore its own connection). Expected outcomes
are exact, because the invariants hold for *every* possible interleaving.
"""

import random
import threading
import uuid

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import DEADLOCK_DETECTED, atomic
from app.core.exceptions import ConcurrencyConflictError
from app.models import SYSTEM_FUNDING_ACCOUNT_ID, Account, TransferKind
from tests.integration.support import (
    balance,
    classify,
    create_account,
    idempotency_record,
    ledger_entry_count,
    merchant_transfer_count,
    reconcile,
    run_concurrently,
    transfer,
    wait_for_lock_waiters,
)

Factory = sessionmaker[Session]


def test_100_concurrent_withdrawals_never_overdraw(concurrent_factory: Factory) -> None:
    """Requirement 1: $100 balance, 100 concurrent $10 transfers, distinct keys."""
    source = create_account(concurrent_factory, funded=10_000)
    destination = create_account(concurrent_factory)

    results = run_concurrently(
        100, lambda _: transfer(concurrent_factory, source, destination, 1_000)
    )
    outcome = classify(results)

    # Contention failures (lock timeouts) are reported separately from business
    # rejections. With a 30s lock_timeout none are expected: queueing is not failure.
    contention = [e for e in outcome["errors"] if isinstance(e, ConcurrencyConflictError)]
    assert contention == []
    assert outcome["errors"] == []
    assert len(outcome["succeeded"]) == 10
    assert len(outcome["INSUFFICIENT_FUNDS"]) == 90

    assert balance(concurrent_factory, source) == 0
    assert balance(concurrent_factory, destination) == 10_000
    assert merchant_transfer_count(concurrent_factory) == 10
    assert ledger_entry_count(concurrent_factory, kind=TransferKind.TRANSFER) == 20
    assert reconcile(concurrent_factory).ok


def test_insufficient_funds_under_contention_is_decided_on_locked_state(
    concurrent_factory: Factory,
) -> None:
    """Requirement 5: 50 x $3 against $50. Exactly floor(5000/300)=16 succeed.

    Every rejection must report the *final* remaining balance (200). That proves each
    funds check read the row under the lock: a stale read would report a larger balance.
    """
    source = create_account(concurrent_factory, funded=5_000)
    destination = create_account(concurrent_factory)

    outcome = classify(
        run_concurrently(50, lambda _: transfer(concurrent_factory, source, destination, 300))
    )

    assert outcome["errors"] == []
    assert len(outcome["succeeded"]) == 16
    rejections = outcome["INSUFFICIENT_FUNDS"]
    assert len(rejections) == 34
    reported = {r.response_body["error"]["details"]["balance_minor"] for r in rejections}
    assert reported == {200}
    assert balance(concurrent_factory, source) == 200
    assert balance(concurrent_factory, destination) == 4_800


def test_opposite_direction_transfers_do_not_deadlock(concurrent_factory: Factory) -> None:
    """Requirement 6: A->B and B->A at the same time. Locks are always taken in ID order,
    so the two directions queue on the same first lock instead of deadlocking."""
    a = create_account(concurrent_factory, funded=10_000, name="A")
    b = create_account(concurrent_factory, funded=10_000, name="B")

    def task(i: int):  # type: ignore[no-untyped-def]
        src, dst = (a, b) if i % 2 == 0 else (b, a)
        return transfer(concurrent_factory, src, dst, 100)

    outcome = classify(run_concurrently(100, task))

    assert outcome["errors"] == []  # in particular: no deadlock-victim conflicts
    assert len(outcome["succeeded"]) == 100
    assert balance(concurrent_factory, a) == 10_000
    assert balance(concurrent_factory, b) == 10_000
    assert reconcile(concurrent_factory).ok


def test_balance_conservation_under_random_concurrent_mesh(concurrent_factory: Factory) -> None:
    """Requirement 11: 100 concurrent transfers between 5 accounts in random directions.

    Which transfers succeed depends on timing; the invariants do not.
    """
    accounts = [create_account(concurrent_factory, funded=2_000, name=f"M{i}") for i in range(5)]
    rng = random.Random(1234)
    plan = []
    for _ in range(100):
        src, dst = rng.sample(accounts, 2)
        plan.append((src, dst, rng.randint(1, 900)))

    outcome = classify(run_concurrently(100, lambda i: transfer(concurrent_factory, *plan[i])))

    assert outcome["errors"] == []
    assert set(outcome) <= {"succeeded", "errors", "INSUFFICIENT_FUNDS"}
    balances = [balance(concurrent_factory, a) for a in accounts]
    assert sum(balances) == 10_000  # merchant money conserved
    assert all(b >= 0 for b in balances)
    assert balance(concurrent_factory, SYSTEM_FUNDING_ACCOUNT_ID) == -10_000
    succeeded = len(outcome["succeeded"])
    assert merchant_transfer_count(concurrent_factory) == succeeded
    assert ledger_entry_count(concurrent_factory, kind=TransferKind.TRANSFER) == 2 * succeeded
    report = reconcile(concurrent_factory)
    assert report.ok, report


def test_concurrent_funding_does_not_lose_updates(concurrent_factory: Factory) -> None:
    """Phase 1 funding was read-modify-write without locks; now it is a locked posting."""
    from app.services.account_service import AccountService

    account = create_account(concurrent_factory)

    def fund(_: int) -> None:
        with concurrent_factory() as session:
            AccountService(session, lock_timeout_ms=30_000).fund_account(account, 100)

    results = run_concurrently(50, fund)

    assert [r for r in results if isinstance(r, BaseException)] == []
    assert balance(concurrent_factory, account) == 5_000
    assert balance(concurrent_factory, SYSTEM_FUNDING_ACCOUNT_ID) == -5_000


def test_lock_timeout_is_a_retryable_conflict_and_persists_nothing(
    session_factory: Factory, engine: Engine
) -> None:
    source = create_account(session_factory, funded=1_000)
    destination = create_account(session_factory)
    key = str(uuid.uuid4())

    # Another transaction holds the source row lock and does not let go.
    with engine.connect() as holder:
        holder_tx = holder.begin()
        holder.execute(text("SELECT 1 FROM accounts WHERE id = :id FOR UPDATE"), {"id": source})

        with pytest.raises(ConcurrencyConflictError) as exc_info:
            transfer(session_factory, source, destination, 100, key=key, lock_timeout_ms=200)
        holder_tx.rollback()

    assert exc_info.value.details["sqlstate"] == "55P03"
    assert idempotency_record(session_factory, key) is None  # claim rolled back
    assert merchant_transfer_count(session_factory) == 0
    assert balance(session_factory, source) == 1_000

    # Retrying the SAME key once the lock is free executes exactly once.
    result = transfer(session_factory, source, destination, 100, key=key)
    assert result.succeeded and not result.replayed
    assert balance(session_factory, source) == 900


def test_inconsistent_lock_order_deadlocks_and_is_reported_as_conflict(
    session_factory: Factory, engine: Engine
) -> None:
    """Why lock ordering matters: lock A then B in one transaction and B then A in another,
    and PostgreSQL must abort one of them (deadlock_timeout, 1s by default). The victim
    surfaces as a retryable ConcurrencyConflictError. LedgerPoster avoids this entirely by
    always locking in ascending ID order."""
    a = create_account(session_factory, name="A")
    b = create_account(session_factory, name="B")
    first_locked = {a: threading.Event(), b: threading.Event()}

    def lock(session: Session, account_id: uuid.UUID) -> None:
        session.execute(select(Account).where(Account.id == account_id).with_for_update())

    def worker(first: uuid.UUID, second: uuid.UUID) -> BaseException | None:
        try:
            with session_factory() as session, atomic(session, lock_timeout_ms=30_000):
                lock(session, first)
                first_locked[first].set()
                assert first_locked[second].wait(15)
                if first == b:  # second worker: only proceed once the first is blocked
                    wait_for_lock_waiters(engine, count=1)
                lock(session, second)
        except BaseException as exc:
            return exc
        return None

    results: list[BaseException | None] = [None, None]
    threads = [
        threading.Thread(target=lambda: results.__setitem__(0, worker(a, b))),
        threading.Thread(target=lambda: results.__setitem__(1, worker(b, a))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    victims = [r for r in results if r is not None]
    assert len(victims) == 1, results
    assert isinstance(victims[0], ConcurrencyConflictError)
    assert victims[0].details["sqlstate"] == DEADLOCK_DETECTED
