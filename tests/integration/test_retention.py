"""Retention against the real test database, run as the least-privilege runtime role."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.maintenance import retention
from app.models import LabRun, LedgerEntry, Transfer, WorkerHeartbeat
from tests.integration.support import create_account, reconcile, transfer

Factory = sessionmaker[Session]
OLD = datetime.now(UTC) - timedelta(days=400)
RECENT = datetime.now(UTC) - timedelta(hours=1)


def _lab_run(session: Session, *, status: str, at: datetime) -> uuid.UUID:
    run_id = uuid.uuid4()
    session.add(
        LabRun(
            id=run_id,
            experiment="concurrency",
            status=status,
            params={},
            progress={},
            runner_replica="test",
            target_base_url="http://127.0.0.1:8000",
            created_at=at,
            finished_at=at if status not in ("queued", "running") else None,
            heartbeat_at=at,
        )
    )
    session.flush()
    session.execute(
        text("INSERT INTO lab_run_events (run_id, seq, payload) VALUES (:r, 1, '{}')"),
        {"r": run_id},
    )
    return run_id


def _heartbeat(session: Session, instance: str, at: datetime) -> None:
    session.add(
        WorkerHeartbeat(
            component="event-consumer",
            instance=instance,
            status="ok",
            details={},
            started_at=at,
            beat_at=at,
            next_beat_before=at + timedelta(seconds=15),
        )
    )


def _seed(factory: Factory) -> dict[str, uuid.UUID]:
    with factory() as session, session.begin():
        ids = {
            "old_done": _lab_run(session, status="passed", at=OLD),
            "old_failed": _lab_run(session, status="failed", at=OLD),
            "old_but_running": _lab_run(session, status="running", at=OLD),
            "recent_done": _lab_run(session, status="passed", at=RECENT),
        }
        _heartbeat(session, "gone", OLD)
        _heartbeat(session, "alive", RECENT)
    source = create_account(factory, funded=1_000)
    destination = create_account(factory)
    transfer(factory, source, destination, 250)
    return ids


def _counts(factory: Factory) -> dict[str, int]:
    with factory() as session:
        return {
            "transfers": session.scalar(select(func.count()).select_from(Transfer)) or 0,
            "ledger": session.scalar(select(func.count()).select_from(LedgerEntry)) or 0,
            "runs": session.scalar(select(func.count()).select_from(LabRun)) or 0,
            "events": session.scalar(text("SELECT count(*) FROM lab_run_events")) or 0,
            "heartbeats": session.scalar(select(func.count()).select_from(WorkerHeartbeat)) or 0,
        }


def _runtime_engine(test_settings: Settings) -> Engine:
    return create_engine(test_settings.database_url)  # the runtime role, as in the stack


def test_dry_run_counts_and_deletes_nothing(
    session_factory: Factory, test_settings: Settings
) -> None:
    _seed(session_factory)
    before = _counts(session_factory)
    engine = _runtime_engine(test_settings)
    try:
        result = retention.run(engine, test_settings, dry_run=True)
    finally:
        engine.dispose()
    assert result == {"lab_runs": 2, "worker_heartbeats": 1}
    assert _counts(session_factory) == before


def test_prunes_only_old_finished_runs_and_stale_heartbeats(
    session_factory: Factory, test_settings: Settings
) -> None:
    ids = _seed(session_factory)
    before = _counts(session_factory)
    engine = _runtime_engine(test_settings)
    try:
        result = retention.run(engine, test_settings, dry_run=False)
        again = retention.run(engine, test_settings, dry_run=False)  # idempotent
    finally:
        engine.dispose()
    assert result == {"lab_runs": 2, "worker_heartbeats": 1}
    assert again == {"lab_runs": 0, "worker_heartbeats": 0}
    after = _counts(session_factory)
    assert after["transfers"] == before["transfers"] and after["ledger"] == before["ledger"]
    assert after["runs"] == before["runs"] - 2
    assert after["events"] == before["events"] - 2  # cascaded with their runs
    assert after["heartbeats"] == before["heartbeats"] - 1
    with session_factory() as session:
        remaining = set(session.scalars(select(LabRun.id)))
    assert ids["old_but_running"] in remaining  # an active run is never pruned
    assert ids["recent_done"] in remaining
    assert reconcile(session_factory).ok


def test_no_policy_targets_a_financial_or_audit_table() -> None:
    assert {p.table for p in retention.POLICIES}.isdisjoint(retention.PROTECTED_TABLES)
    assert {p.table for p in retention.POLICIES} == {"lab_runs", "worker_heartbeats"}
