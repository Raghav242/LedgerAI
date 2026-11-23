import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AnomalyResult(Base):
    """The AI worker's durable effect: one model prediction per transfer.completed event.

    Written in the same transaction as the worker's processed_events receipt; event_id is
    the primary key and transfer_id is unique, so a duplicate delivery cannot add a row.
    `score` is relative unusualness, NOT a probability.
    """

    __tablename__ = "anomaly_results"
    __table_args__ = (
        Index("ix_anomaly_results_scored_at", "scored_at"),
        Index("ix_anomaly_results_source_account_id", "source_account_id"),
    )

    event_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    transfer_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    source_account_id: Mapped[uuid.UUID] = mapped_column()
    model_version: Mapped[str] = mapped_column(String(40))
    score: Mapped[float] = mapped_column(Float)
    threshold: Mapped[float] = mapped_column(Float)
    is_anomaly: Mapped[bool] = mapped_column(Boolean)
    features: Mapped[dict[str, Any]] = mapped_column(JSONB)
    explanation: Mapped[list[str]] = mapped_column(JSONB)
    rule_flags: Mapped[list[str]] = mapped_column(JSONB)
    inference_ms: Mapped[float] = mapped_column(Float)
    delivery_attempt: Mapped[int] = mapped_column(Integer)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
