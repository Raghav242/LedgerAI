import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class IdempotencyRecord(Base):
    """The final outcome of one idempotent request, keyed by (scope, idempotency_key).

    The row is inserted (claimed) and completed inside the SAME transaction as the
    transfer it protects. Other transactions therefore only ever see completed rows:
    an uncommitted claim is invisible to them and only shows up as a lock on the unique
    index, which makes concurrent same-key requests wait. If the process crashes,
    PostgreSQL aborts the transaction and the claim disappears with it, so there is
    never an orphaned IN_PROGRESS row to recover.
    """

    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("scope", "idempotency_key", name="uq_idempotency_records_scope_key"),
        CheckConstraint("outcome IN ('succeeded', 'rejected')", name="outcome_valid"),
        CheckConstraint(
            "outcome IS NULL OR response_body IS NOT NULL", name="completed_has_response"
        ),
        CheckConstraint(
            "outcome IS DISTINCT FROM 'succeeded' OR transfer_id IS NOT NULL",
            name="success_has_transfer",
        ),
        CheckConstraint(
            "outcome IS DISTINCT FROM 'rejected' OR error_code IS NOT NULL",
            name="rejection_has_error_code",
        ),
    )
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    scope: Mapped[str] = mapped_column(String(100))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    # SHA-256 hex of the canonical request (see services/idempotency.py).
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    # NULL only while the claiming transaction is still running (never visible after commit).
    outcome: Mapped[str | None] = mapped_column(String(20))
    error_code: Mapped[str | None] = mapped_column(String(50))
    transfer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transfers.id", ondelete="RESTRICT")
    )
    # Plain JSON (not JSONB) on purpose: PostgreSQL stores json text verbatim, so a
    # replay returns byte-for-byte the same body (JSONB would reorder object keys).
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
