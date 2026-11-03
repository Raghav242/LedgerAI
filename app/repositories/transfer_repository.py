import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Transfer


class TransferRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, transfer: Transfer) -> Transfer:
        self._session.add(transfer)
        self._session.flush()
        return transfer

    def list_all(self, *, account_id: uuid.UUID | None, limit: int, offset: int) -> list[Transfer]:
        stmt = select(Transfer)
        if account_id is not None:
            stmt = stmt.where(
                or_(
                    Transfer.source_account_id == account_id,
                    Transfer.destination_account_id == account_id,
                )
            )
        stmt = stmt.order_by(Transfer.created_at.desc(), Transfer.id).limit(limit).offset(offset)
        return list(self._session.scalars(stmt))
