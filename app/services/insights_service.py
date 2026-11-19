"""Read-only views over the ledger for dashboards: paged postings and system aggregates.

Nothing here writes. Aggregates run in a REPEATABLE READ, read-only transaction so every
number in one summary describes the same snapshot (the same technique as reconciliation).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy.orm import Session

from app.models import LedgerEntry, TransferKind
from app.repositories.account_repository import AccountRepository
from app.repositories.ledger_repository import LedgerRepository
from app.repositories.transfer_repository import TransferRepository

MAX_ACTIVITY_DAYS = 90


@dataclass(frozen=True, slots=True)
class KindTotals:
    kind: TransferKind
    count: int
    volume_minor: int


@dataclass(frozen=True, slots=True)
class DailyTotals:
    day: date
    count: int
    volume_minor: int


@dataclass(frozen=True, slots=True)
class SystemSummary:
    merchant_account_count: int
    merchant_balance_total_minor: int
    ledger_entry_count: int
    transfers_by_kind: list[KindTotals]
    daily_transfer_activity: list[DailyTotals]
    generated_at: datetime


class InsightsService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._accounts = AccountRepository(session)
        self._transfers = TransferRepository(session)
        self._ledger = LedgerRepository(session)

    def list_ledger_entries(
        self, *, account_id: uuid.UUID | None, limit: int, offset: int
    ) -> list[LedgerEntry]:
        return self._ledger.list_entries(account_id=account_id, limit=limit, offset=offset)

    def summary(self, *, activity_days: int, now: datetime | None = None) -> SystemSummary:
        if not 1 <= activity_days <= MAX_ACTIVITY_DAYS:
            raise ValueError(f"activity_days must be between 1 and {MAX_ACTIVITY_DAYS}")
        now = now or datetime.now(UTC)
        first_day = now.astimezone(UTC).date() - timedelta(days=activity_days - 1)
        since = datetime.combine(first_day, time.min, tzinfo=UTC)

        # Must be the first statement on this session: isolation is fixed per transaction.
        self._session.connection(
            execution_options={"isolation_level": "REPEATABLE READ", "postgresql_readonly": True}
        )
        try:
            account_count, balance_total = self._accounts.merchant_totals()
            _, _, _, entry_count = self._ledger.totals()
            by_kind = self._transfers.totals_by_kind()
            daily = self._transfers.daily_totals(kind=TransferKind.TRANSFER, since=since)
        finally:
            self._session.rollback()  # read-only: end the snapshot transaction

        return SystemSummary(
            merchant_account_count=account_count,
            merchant_balance_total_minor=balance_total,
            ledger_entry_count=entry_count,
            transfers_by_kind=[
                KindTotals(kind, *by_kind.get(kind.value, (0, 0))) for kind in TransferKind
            ],
            daily_transfer_activity=[
                DailyTotals(day, *daily.get(day, (0, 0)))
                for day in (first_day + timedelta(days=i) for i in range(activity_days))
            ],
            generated_at=now,
        )
