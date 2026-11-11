import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Account, LedgerEntry, LedgerEntryRole, Transfer


@dataclass(frozen=True, slots=True)
class AccountBalanceMismatch:
    account_id: uuid.UUID
    balance_minor: int
    ledger_balance_minor: int


class LedgerRepository:
    """Append-only access to ledger_entries plus the read queries used for reconciliation."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add_postings(self, transfer: Transfer) -> list[LedgerEntry]:
        """Write the debit/credit pair for a transfer (flushed, not committed)."""
        entries = [
            LedgerEntry(
                transfer_id=transfer.id,
                account_id=transfer.source_account_id,
                role=LedgerEntryRole.DEBIT,
                amount_minor=-transfer.amount_minor,
                currency=transfer.currency,
            ),
            LedgerEntry(
                transfer_id=transfer.id,
                account_id=transfer.destination_account_id,
                role=LedgerEntryRole.CREDIT,
                amount_minor=transfer.amount_minor,
                currency=transfer.currency,
            ),
        ]
        self._session.add_all(entries)
        self._session.flush()
        return entries

    def entries_for_transfer(self, transfer_id: uuid.UUID) -> list[LedgerEntry]:
        stmt = (
            select(LedgerEntry)
            .where(LedgerEntry.transfer_id == transfer_id)
            .order_by(LedgerEntry.role.desc())  # debit before credit
        )
        return list(self._session.scalars(stmt))

    def ledger_balance(self, account_id: uuid.UUID) -> int:
        stmt = select(func.coalesce(func.sum(LedgerEntry.amount_minor), 0)).where(
            LedgerEntry.account_id == account_id
        )
        return int(self._session.scalar(stmt) or 0)

    # --- reconciliation ---------------------------------------------------------------

    def find_balance_mismatches(self) -> list[AccountBalanceMismatch]:
        """Accounts whose stored balance differs from the sum of their ledger entries."""
        ledger_sums = (
            select(
                LedgerEntry.account_id.label("account_id"),
                func.sum(LedgerEntry.amount_minor).label("total"),
            )
            .group_by(LedgerEntry.account_id)
            .subquery()
        )
        ledger_total = func.coalesce(ledger_sums.c.total, 0)
        stmt = (
            select(Account.id, Account.balance_minor, ledger_total)
            .outerjoin(ledger_sums, ledger_sums.c.account_id == Account.id)
            .where(Account.balance_minor != ledger_total)
            .order_by(Account.id)
        )
        return [
            AccountBalanceMismatch(
                account_id=row[0], balance_minor=row[1], ledger_balance_minor=row[2]
            )
            for row in self._session.execute(stmt)
        ]

    def find_unbalanced_transfers(self) -> list[uuid.UUID]:
        """Transfers that do not have exactly two entries summing to zero."""
        stmt = (
            select(Transfer.id)
            .outerjoin(LedgerEntry, LedgerEntry.transfer_id == Transfer.id)
            .group_by(Transfer.id)
            .having(
                (func.count(LedgerEntry.id) != 2)
                | (func.coalesce(func.sum(LedgerEntry.amount_minor), 0) != 0)
            )
            .order_by(Transfer.id)
        )
        return list(self._session.scalars(stmt))

    def totals(self) -> tuple[int, int, int, int]:
        """(sum of all ledger entries, sum of all account balances, #accounts, #entries)."""
        ledger_total, entry_count = self._session.execute(
            select(func.coalesce(func.sum(LedgerEntry.amount_minor), 0), func.count(LedgerEntry.id))
        ).one()
        balance_total, account_count = self._session.execute(
            select(func.coalesce(func.sum(Account.balance_minor), 0), func.count(Account.id))
        ).one()
        return int(ledger_total), int(balance_total), int(account_count), int(entry_count)
