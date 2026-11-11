"""Requirement 12: retries after process restarts, crashes and ambiguous commits."""

import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.exceptions import CommitOutcomeUnknownError
from app.main import create_app
from app.repositories.idempotency_repository import IdempotencyRepository
from app.services.transfer_service import TransferCommand, TransferService
from tests.integration.conftest import create_account as api_create_account
from tests.integration.conftest import fund_account as api_fund_account
from tests.integration.conftest import get_balance, post_transfer
from tests.integration.support import (
    balance,
    create_account,
    idempotency_record,
    ledger_entry_count,
    merchant_transfer_count,
    reconcile,
    transfer,
)

Factory = sessionmaker[Session]


def test_retry_after_api_process_restart_replays_committed_result(
    test_settings: Settings, session_factory: Factory
) -> None:
    """Commit, 'kill' the API process (new app, new engine, empty pool), retry the key."""
    key = str(uuid.uuid4())
    with TestClient(create_app(test_settings)) as before_restart:
        a, b = api_create_account(before_restart), api_create_account(before_restart)
        api_fund_account(before_restart, a["id"], 1_000)
        original = post_transfer(before_restart, a["id"], b["id"], 300, idempotency_key=key)
    # The first app's lifespan has ended and its engine is disposed: nothing in memory
    # survives. Only PostgreSQL remembers the key.
    with TestClient(create_app(test_settings)) as after_restart:
        retry = post_transfer(after_restart, a["id"], b["id"], 300, idempotency_key=key)
        assert get_balance(after_restart, a["id"]) == 700

    assert original.status_code == retry.status_code == 201
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert retry.content == original.content
    assert merchant_transfer_count(session_factory) == 1


def test_crash_mid_transaction_leaves_no_orphaned_claim(
    session_factory: Factory, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request dies after balances, transfer and ledger rows were written but before
    COMMIT. PostgreSQL aborts the transaction; the claim vanishes with it; the retry with
    the same key executes exactly once. There is no IN_PROGRESS state to recover."""
    source = create_account(session_factory, funded=1_000)
    destination = create_account(session_factory)
    key = str(uuid.uuid4())
    written, release = threading.Event(), threading.Event()
    real_complete = IdempotencyRepository.complete

    def pause_before_completion(self, record, **kwargs):  # type: ignore[no-untyped-def]
        written.set()
        assert release.wait(30)
        return real_complete(self, record, **kwargs)

    monkeypatch.setattr(IdempotencyRepository, "complete", pause_before_completion)
    box: dict[str, object] = {}

    def doomed() -> None:
        try:
            box["result"] = transfer(session_factory, source, destination, 400, key=key)
        except BaseException as exc:
            box["result"] = exc

    worker = threading.Thread(target=doomed)
    worker.start()
    assert written.wait(15)

    # Simulate the API process being killed: the server terminates its backend.
    with engine.connect() as admin:
        terminated = admin.scalar(
            text(
                "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
                "WHERE datname = current_database() AND state = 'idle in transaction' "
                "AND pid <> pg_backend_pid()"
            )
        )
    assert terminated == 1
    release.set()
    worker.join(30)
    assert isinstance(box["result"], OperationalError)
    monkeypatch.undo()

    # Nothing survived the crash...
    assert idempotency_record(session_factory, key) is None
    assert balance(session_factory, source) == 1_000
    assert merchant_transfer_count(session_factory) == 0
    # ...so the retry runs once.
    retry = transfer(session_factory, source, destination, 400, key=key)
    assert retry.succeeded and not retry.replayed
    assert balance(session_factory, source) == 600
    assert merchant_transfer_count(session_factory) == 1
    assert reconcile(session_factory).ok


def test_connection_lost_during_commit_is_reported_as_unknown_and_retry_is_safe(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worst case: COMMIT reached the server and succeeded, but the reply was lost.
    The service cannot know, says so, and the same-key retry reveals the truth."""
    source = create_account(session_factory, funded=1_000)
    destination = create_account(session_factory)
    key = str(uuid.uuid4())
    command = TransferCommand(source, destination, 250, "USD")

    with session_factory() as session:
        real_commit = session.commit

        def commit_then_lose_reply() -> None:
            real_commit()
            raise OperationalError(
                "COMMIT", {}, Exception("server closed the connection"), connection_invalidated=True
            )

        monkeypatch.setattr(session, "commit", commit_then_lose_reply)
        with pytest.raises(CommitOutcomeUnknownError):
            TransferService(session, lock_timeout_ms=5_000).create_transfer(
                scope="demo-merchant", idempotency_key=key, command=command
            )

    # The client retries with the SAME key (never a new one): replay, no second transfer.
    retry = transfer(session_factory, source, destination, 250, key=key)
    assert retry.succeeded and retry.replayed
    assert balance(session_factory, source) == 750
    assert merchant_transfer_count(session_factory) == 1
    assert ledger_entry_count(session_factory) == 4  # funding pair + transfer pair
