import uuid
from datetime import date, datetime

from sqlalchemy import Date, cast, func, or_, select
from sqlalchemy.orm import Session

from app.models import Transfer, TransferKind


class TransferRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, transfer: Transfer) -> Transfer:
        self._session.add(transfer)
        self._session.flush()
        return transfer

    def get(self, transfer_id: uuid.UUID) -> Transfer | None:
        return self._session.get(Transfer, transfer_id)

    def totals_by_kind(self) -> dict[str, tuple[int, int]]:
        """{kind: (count, sum of amount_minor)} for every kind that has transfers."""
        stmt = select(
            Transfer.kind,
            func.count(Transfer.id),
            func.coalesce(func.sum(Transfer.amount_minor), 0),
        ).group_by(Transfer.kind)
        return {
            kind: (int(count), int(volume)) for kind, count, volume in self._session.execute(stmt)
        }

    def daily_totals(self, *, kind: TransferKind, since: datetime) -> dict[date, tuple[int, int]]:
        """{UTC day: (count, volume)} for transfers of `kind` created at or after `since`.

        Range-filtered on the indexed created_at column, so the cost is proportional to
        the window, not to the whole history.
        """
        day = cast(func.timezone("UTC", Transfer.created_at), Date)
        stmt = (
            select(day, func.count(Transfer.id), func.coalesce(func.sum(Transfer.amount_minor), 0))
            .where(Transfer.kind == kind, Transfer.created_at >= since)
            .group_by(day)
        )
        return {d: (int(count), int(volume)) for d, count, volume in self._session.execute(stmt)}

    def list_all(
        self,
        *,
        account_id: uuid.UUID | None,
        kind: TransferKind | None,
        limit: int,
        offset: int,
    ) -> list[Transfer]:
        stmt = select(Transfer)
        if account_id is not None:
            stmt = stmt.where(
                or_(
                    Transfer.source_account_id == account_id,
                    Transfer.destination_account_id == account_id,
                )
            )
        if kind is not None:
            stmt = stmt.where(Transfer.kind == kind)
        stmt = stmt.order_by(Transfer.created_at.desc(), Transfer.id).limit(limit).offset(offset)
        return list(self._session.scalars(stmt))
