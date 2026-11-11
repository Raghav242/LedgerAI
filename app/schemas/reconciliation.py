import uuid

from pydantic import BaseModel


class BalanceMismatchRead(BaseModel):
    account_id: uuid.UUID
    balance_minor: int
    ledger_balance_minor: int


class ReconciliationRead(BaseModel):
    ok: bool
    accounts_checked: int
    ledger_entries_checked: int
    ledger_total_minor: int
    balance_total_minor: int
    balance_mismatches: list[BalanceMismatchRead]
    unbalanced_transfer_ids: list[uuid.UUID]
