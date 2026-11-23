from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, PrimaryKeyConstraint, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

WORKER_STATUSES = ("ok", "degraded", "error")


class WorkerHeartbeat(Base):
    """Latest self-report of one background worker instance (Phase 5C observability).

    Written by the worker's own main loop (upsert), so a fresh row proves the loop is
    running AND can reach PostgreSQL -- unlike "the container exists". `next_beat_before`
    is the worker's own promise of when it will report again (it knows when it is about to
    back off); a row past that deadline is STALE, never shown as current.
    """

    __tablename__ = "worker_heartbeats"
    __table_args__ = (
        CheckConstraint("status IN ('ok', 'degraded', 'error')", name="status_valid"),
        PrimaryKeyConstraint("component", "instance", name="pk_worker_heartbeats"),
    )

    component: Mapped[str] = mapped_column(String(60))
    instance: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    beat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    next_beat_before: Mapped[datetime] = mapped_column(DateTime(timezone=True))
