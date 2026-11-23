"""Worker self-reports for the live topology (Phase 5C).

A worker's main loop calls `beat()` every iteration; at most one row write per interval
reaches PostgreSQL (upsert of its own row in `worker_heartbeats`). Each beat states when
the next one is due (`next_beat_before`): normally a few intervals ahead, longer when the
worker knows it is about to back off. Readers compare that deadline with the database
clock, so a worker that hangs or dies turns STALE instead of staying "healthy".

Reporting must never disturb the worker: every failure is swallowed (and the row simply
goes stale, which is the truth: the worker cannot reach PostgreSQL).
"""

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.models.worker_heartbeat import WorkerHeartbeat

logger = logging.getLogger(__name__)

HeartbeatStatus = Literal["ok", "degraded", "error"]
DEFAULT_INTERVAL_SECONDS = 5.0
# A beat is overdue after this many intervals (plus any announced back-off).
GRACE_INTERVALS = 3
MAX_ERROR_LENGTH = 500


class HeartbeatReporter:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        component: str,
        instance: str,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sessions = session_factory
        self.component = component
        self.instance = instance[:100]
        self._interval = interval_seconds
        self._clock = clock
        self._started_at = datetime.now(UTC)
        self._last_sent: float | None = None
        self._last_error: str | None = None
        self._last_error_at: datetime | None = None
        self._last_ok_at: datetime | None = None

    def record_error(self, error: str) -> None:
        """Remember the most recent problem; it is shown with every later beat."""
        self._last_error = error[:MAX_ERROR_LENGTH]
        self._last_error_at = datetime.now(UTC)

    def due(self) -> bool:
        """True if the next (non-forced) beat would be written."""
        return self._last_sent is None or self._clock() - self._last_sent >= self._interval

    def beat(
        self,
        status: HeartbeatStatus,
        details: dict[str, Any],
        *,
        force: bool = False,
        next_within_seconds: float = 0.0,
    ) -> bool:
        """Write the heartbeat if one is due (or `force`). Returns True if written."""
        now = self._clock()
        if not force and self._last_sent is not None and now - self._last_sent < self._interval:
            return False
        deadline = self._interval * GRACE_INTERVALS + max(0.0, next_within_seconds)
        if status == "ok":
            self._last_ok_at = datetime.now(UTC)
        if self._last_ok_at is not None:
            details = {**details, "last_ok_at": self._last_ok_at.isoformat()}
        values = {
            "status": status,
            "details": details,
            "last_error": self._last_error,
            "last_error_at": self._last_error_at,
            "beat_at": func.now(),
            "next_beat_before": func.now() + func.make_interval(0, 0, 0, 0, 0, 0, deadline),
        }
        try:
            with self._sessions() as session, session.begin():
                session.execute(
                    insert(WorkerHeartbeat)
                    .values(
                        component=self.component,
                        instance=self.instance,
                        started_at=self._started_at,
                        **values,
                    )
                    .on_conflict_do_update(
                        constraint="pk_worker_heartbeats",
                        set_={**values, "started_at": self._started_at},
                    )
                )
        except (SQLAlchemyError, OSError) as exc:
            logger.debug("Heartbeat for %s not recorded: %r", self.component, exc)
            return False
        self._last_sent = now
        return True
