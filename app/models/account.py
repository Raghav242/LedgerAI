import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import AccountKind

# Well-known ID of the system account that dev funding is drawn from. Created by
# migration 0002 (which hardcodes the same literal, as migrations must not import app code).
SYSTEM_FUNDING_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        # Defense in depth: the service checks funds under a row lock first, but the
        # database is the last line that refuses to store a negative merchant balance.
        # Only the system issuance account may go negative (see AccountKind.SYSTEM).
        CheckConstraint("balance_minor >= 0 OR kind = 'system'", name="balance_non_negative"),
        CheckConstraint("currency IN ('USD')", name="currency_supported"),
        CheckConstraint("kind IN ('merchant', 'system')", name="kind_valid"),
    )
    # Fetch server-generated values (created_at) via INSERT ... RETURNING.
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100))
    currency: Mapped[str] = mapped_column(String(3))
    kind: Mapped[str] = mapped_column(
        String(20), default=AccountKind.MERCHANT, server_default=text("'merchant'")
    )
    # Integer minor units (cents for USD). BIGINT allows ~9.2e18 cents.
    balance_minor: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @property
    def is_system(self) -> bool:
        return self.kind == AccountKind.SYSTEM

    def __repr__(self) -> str:
        return f"Account(id={self.id}, balance_minor={self.balance_minor} {self.currency})"
