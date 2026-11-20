import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

LAB_RUN_STATUSES = ("queued", "running", "passed", "failed", "error")
LAB_RUN_ACTIVE_STATUSES = ("queued", "running")


class LabRun(Base):
    """One Experiment Lab run (Phase 5B). Lab bookkeeping only: no financial data.

    Stored in PostgreSQL rather than in process memory because the run is started on
    whichever replica NGINX picked, and the browser's status polls land on any replica.

    Lifecycle: queued -> running -> passed | failed | error.
      passed  completed and every invariant held
      failed  completed but at least one invariant was violated
      error   could not complete (timeout, unreachable target, runner stopped)

    At most one run may be active at a time (partial unique index below): concurrent
    experiments would distort each other's measurements. A runner that dies leaves a
    stale heartbeat; the next run start marks such a run as `error`.
    """

    __tablename__ = "lab_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'passed', 'failed', 'error')", name="status_valid"
        ),
        Index(
            "uq_lab_runs_one_active",
            text("(true)"),
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        Index("ix_lab_runs_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    experiment: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    progress: Mapped[dict[str, Any]] = mapped_column(JSONB)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    checks: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    runner_replica: Mapped[str] = mapped_column(String(100))
    target_base_url: Mapped[str] = mapped_column(String(200))
    event_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    events_dropped: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class LabRunEvent(Base):
    """Timeline of a run: phases, individual observed requests, checks. Append-only."""

    __tablename__ = "lab_run_events"
    __table_args__ = (PrimaryKeyConstraint("run_id", "seq"),)

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("lab_runs.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
