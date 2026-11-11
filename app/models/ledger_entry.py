import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class LedgerEntry(Base):
    """One side of a double-entry posting. Append-only.

    Each transfer has exactly one debit (negative, source account) and one credit
    (positive, destination account) whose amounts sum to zero. A row-level CHECK enforces
    the sign, the unique constraint prevents a second posting for the same role, and
    deferred constraint triggers (migration 0002) verify the pair at COMMIT and reject
    any UPDATE or DELETE.
    """

    __tablename__ = "ledger_entries"
    __table_args__ = (
        UniqueConstraint("transfer_id", "role", name="uq_ledger_entries_transfer_id_role"),
        CheckConstraint("role IN ('debit', 'credit')", name="role_valid"),
        CheckConstraint(
            "(role = 'debit' AND amount_minor < 0) OR (role = 'credit' AND amount_minor > 0)",
            name="sign_matches_role",
        ),
        CheckConstraint("currency IN ('USD')", name="currency_supported"),
    )
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # Covered by the (transfer_id, role) unique index, so no separate index is needed.
    transfer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transfers.id", ondelete="RESTRICT"))
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), index=True
    )
    role: Mapped[str] = mapped_column(String(10))
    # Signed integer minor units: negative = debit, positive = credit.
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
