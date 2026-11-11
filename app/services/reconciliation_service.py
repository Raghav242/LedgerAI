from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.repositories.ledger_repository import AccountBalanceMismatch, LedgerRepository


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    accounts_checked: int
    ledger_entries_checked: int
    ledger_total_minor: int  # must be 0: every posting has an equal, opposite partner
    balance_total_minor: int  # must be 0: the system account offsets all issued money
    balance_mismatches: list[AccountBalanceMismatch] = field(default_factory=list)
    unbalanced_transfer_ids: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            self.ledger_total_minor == 0
            and self.balance_total_minor == 0
            and not self.balance_mismatches
            and not self.unbalanced_transfer_ids
        )


class ReconciliationService:
    """Compares stored balances with ledger-derived balances.

    All queries must see one consistent snapshot, otherwise a transfer committing between
    two queries would show up as a false mismatch. READ COMMITTED takes a new snapshot per
    statement, so this runs in a REPEATABLE READ (single-snapshot) read-only transaction.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._ledger = LedgerRepository(session)

    def run(self) -> ReconciliationReport:
        # Must be the first statement on this session: isolation is fixed per transaction.
        self._session.connection(
            execution_options={"isolation_level": "REPEATABLE READ", "postgresql_readonly": True}
        )
        try:
            ledger_total, balance_total, accounts, entries = self._ledger.totals()
            return ReconciliationReport(
                accounts_checked=accounts,
                ledger_entries_checked=entries,
                ledger_total_minor=ledger_total,
                balance_total_minor=balance_total,
                balance_mismatches=self._ledger.find_balance_mismatches(),
                unbalanced_transfer_ids=self._ledger.find_unbalanced_transfers(),
            )
        finally:
            self._session.rollback()  # read-only: end the snapshot transaction
