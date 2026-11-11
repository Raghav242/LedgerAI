import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import TransferKind


class Transfer(Base):
    """An immutable record of one completed movement of money between two accounts.

    Every transfer is backed by exactly two ledger entries (enforced by a deferred
    constraint trigger), and a database trigger rejects UPDATE/DELETE. Corrections in
    later phases will be modelled as new, compensating records.
    """

    __tablename__ = "transfers"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        CheckConstraint("source_account_id <> destination_account_id", name="distinct_accounts"),
        CheckConstraint("currency IN ('USD')", name="currency_supported"),
        CheckConstraint("status IN ('completed')", name="status_valid"),
        CheckConstraint(
            "kind IN ('transfer', 'dev_funding', 'legacy_opening_balance')", name="kind_valid"
        ),
    )
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # RESTRICT: an account with transfer history can never be deleted out from under it.
    source_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), index=True
    )
    destination_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), index=True
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(20))
    kind: Mapped[str] = mapped_column(
        String(30), default=TransferKind.TRANSFER, server_default=text("'transfer'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
