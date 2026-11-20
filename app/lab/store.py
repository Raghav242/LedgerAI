"""Persistence of lab runs (PostgreSQL), shared by every replica.

Only the replica that started a run writes to it (from its runner thread); any replica
reads it. All writes are short, single-statement-ish transactions on lab tables only.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import sqlstate_of
from app.lab.errors import LabRunInProgressError, LabRunNotFoundError
from app.models.lab_run import LAB_RUN_ACTIVE_STATUSES, LabRun, LabRunEvent

UNIQUE_VIOLATION = "23505"
# A runner flushes at least every FLUSH_INTERVAL; a heartbeat this old means the runner
# (or its replica) is gone and the run can never finish on its own.
STALE_AFTER = timedelta(seconds=30)
KEEP_FINISHED_RUNS = 50


def _now() -> datetime:
    return datetime.now(UTC)


class LabRunStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    # --- creation -----------------------------------------------------------------------

    def create(
        self,
        *,
        experiment: str,
        params: dict[str, Any],
        runner_replica: str,
        target_base_url: str,
    ) -> LabRun:
        self.expire_stale_runs()
        run = LabRun(
            id=uuid.uuid4(),
            experiment=experiment,
            status="queued",
            params=params,
            progress={
                "phase": "queued",
                "message": "Waiting for the runner",
                "completed": 0,
                "total": 0,
            },
            runner_replica=runner_replica,
            target_base_url=target_base_url,
            event_count=0,
            events_dropped=0,
            heartbeat_at=_now(),
        )
        with self._session_factory() as session:
            session.add(run)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                if sqlstate_of(exc) != UNIQUE_VIOLATION:
                    raise
                active = session.scalars(
                    select(LabRun).where(LabRun.status.in_(LAB_RUN_ACTIVE_STATUSES))
                ).first()
                raise LabRunInProgressError(
                    "Another experiment is already running; runs never overlap because "
                    "they would distort each other's measurements.",
                    {
                        "active_run_id": str(active.id) if active else None,
                        "active_experiment": active.experiment if active else None,
                    },
                ) from exc
        self._prune()
        return run

    def expire_stale_runs(self) -> int:
        """Mark active runs whose runner stopped heart-beating as `error`."""
        with self._session_factory() as session, session.begin():
            result = session.execute(
                update(LabRun)
                .where(
                    LabRun.status.in_(LAB_RUN_ACTIVE_STATUSES),
                    LabRun.heartbeat_at < _now() - STALE_AFTER,
                )
                .values(
                    status="error",
                    error="The runner stopped responding (its replica was restarted or "
                    "stopped). The run did not finish.",
                    finished_at=_now(),
                )
            )
            return result.rowcount

    def _prune(self) -> None:
        """Keep the newest KEEP_FINISHED_RUNS runs (lab bookkeeping only; cascades events)."""
        with self._session_factory() as session, session.begin():
            keep = (
                select(LabRun.id)
                .order_by(LabRun.created_at.desc())
                .limit(KEEP_FINISHED_RUNS)
                .scalar_subquery()
            )
            session.execute(
                delete(LabRun).where(
                    LabRun.id.not_in(keep), LabRun.status.not_in(LAB_RUN_ACTIVE_STATUSES)
                )
            )

    # --- runner writes --------------------------------------------------------------------

    def mark_running(self, run_id: uuid.UUID) -> None:
        with self._session_factory() as session, session.begin():
            session.execute(
                update(LabRun)
                .where(LabRun.id == run_id)
                .values(status="running", started_at=_now(), heartbeat_at=_now())
            )

    def flush(
        self,
        run_id: uuid.UUID,
        *,
        events: list[tuple[int, dict[str, Any]]],
        progress: dict[str, Any],
        event_count: int,
        events_dropped: int,
    ) -> None:
        with self._session_factory() as session, session.begin():
            if events:
                session.add_all(
                    LabRunEvent(run_id=run_id, seq=seq, payload=payload) for seq, payload in events
                )
            session.execute(
                update(LabRun)
                .where(LabRun.id == run_id)
                .values(
                    progress=progress,
                    event_count=event_count,
                    events_dropped=events_dropped,
                    heartbeat_at=_now(),
                )
            )

    def finish(
        self,
        run_id: uuid.UUID,
        *,
        status: str,
        result: dict[str, Any] | None,
        checks: list[dict[str, Any]] | None,
        error: str | None,
    ) -> None:
        with self._session_factory() as session, session.begin():
            session.execute(
                update(LabRun)
                .where(LabRun.id == run_id)
                .values(
                    status=status,
                    result=result,
                    checks=checks,
                    error=error,
                    finished_at=_now(),
                    heartbeat_at=_now(),
                )
            )

    # --- reads ------------------------------------------------------------------------------

    def get(self, run_id: uuid.UUID) -> LabRun:
        with self._session_factory() as session:
            run = session.get(LabRun, run_id)
            if run is None:
                raise LabRunNotFoundError("Lab run not found", {"run_id": str(run_id)})
            return run

    def list_recent(self, *, limit: int) -> list[LabRun]:
        with self._session_factory() as session:
            return list(
                session.scalars(select(LabRun).order_by(LabRun.created_at.desc()).limit(limit))
            )

    def events(self, run_id: uuid.UUID, *, after: int, limit: int) -> list[LabRunEvent]:
        with self._session_factory() as session:
            return list(
                session.scalars(
                    select(LabRunEvent)
                    .where(LabRunEvent.run_id == run_id, LabRunEvent.seq > after)
                    .order_by(LabRunEvent.seq)
                    .limit(limit)
                )
            )

    def count_active(self) -> int:
        with self._session_factory() as session:
            return (
                session.scalar(
                    select(func.count())
                    .select_from(LabRun)
                    .where(LabRun.status.in_(LAB_RUN_ACTIVE_STATUSES))
                )
                or 0
            )
