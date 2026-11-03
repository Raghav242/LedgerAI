import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        # Defense in depth: the service checks funds first, but the database is the
        # last line that refuses to store a negative balance no matter what code runs.
        CheckConstraint("balance_minor >= 0", name="balance_non_negative"),
        CheckConstraint("currency IN ('USD')", name="currency_supported"),
    )
    # Fetch server-generated values (created_at) via INSERT ... RETURNING.
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100))
    currency: Mapped[str] = mapped_column(String(3))
    # Integer minor units (cents for USD). BIGINT allows ~9.2e18 cents.
    balance_minor: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"Account(id={self.id}, balance_minor={self.balance_minor} {self.currency})"
