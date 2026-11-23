import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
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


class OutboxEvent(Base):
    """An event to publish, written in the SAME transaction as the change it describes.

    Lifecycle: pending -> processing (claimed under a lease) -> published. A publisher
    that crashes leaves `processing` rows whose `claimed_until` passes; they then count as
    claimable again, so nothing stays stuck. `claim_token` fences a stale publisher: only
    the holder of the current claim can mark the row published or release it.
    """

    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'processing', 'published')", name="status_valid"),
        CheckConstraint("event_version >= 1", name="version_positive"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        # Partial index: the poller only ever looks at unpublished rows, so the index stays
        # small no matter how many events have been published.
        Index(
            "ix_outbox_events_unpublished",
            "available_at",
            postgresql_where=text("status <> 'published'"),
        ),
        # Event Pipeline page: newest-first keyset pages (migration 0005).
        Index("ix_outbox_events_created_at", "created_at", "id"),
    )

    # Deterministic: uuid5(event_type + aggregate operation id). See app/events/ids.py.
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(index=True)
    event_type: Mapped[str] = mapped_column(String(100))
    event_version: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    correlation_id: Mapped[str | None] = mapped_column(String(128))
    # W3C traceparent of the request that committed this event (None when tracing is off).
    # Lets the publisher and consumers continue the same distributed trace.
    trace_parent: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[uuid.UUID | None] = mapped_column()
    last_error: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProcessedEvent(Base):
    """Consumer-side deduplication receipt: event X was handled by consumer Y.

    Inserted in the same transaction as the consumer's durable effect, so "effect applied"
    and "receipt recorded" can never disagree.
    """

    __tablename__ = "processed_events"
    __table_args__ = (
        PrimaryKeyConstraint("event_id", "consumer_name", name="pk_processed_events"),
    )

    event_id: Mapped[uuid.UUID] = mapped_column()
    consumer_name: Mapped[str] = mapped_column(String(100))
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EventAuditEntry(Base):
    """The audit consumer's durable effect: one row per handled event.

    Deliberately NO unique constraint on event_id: deduplication must come from
    processed_events. If it ever failed, duplicates would show up here as extra rows.
    """

    __tablename__ = "event_audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[uuid.UUID] = mapped_column(index=True)
    consumer_name: Mapped[str] = mapped_column(String(100))
    event_type: Mapped[str] = mapped_column(String(100))
    event_version: Mapped[int] = mapped_column(Integer)
    aggregate_id: Mapped[uuid.UUID] = mapped_column()
    amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    correlation_id: Mapped[str | None] = mapped_column(String(128))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    delivery_attempt: Mapped[int] = mapped_column(Integer)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
