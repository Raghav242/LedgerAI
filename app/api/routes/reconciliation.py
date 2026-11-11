from fastapi import APIRouter

from app.api.dependencies import ReconciliationServiceDep
from app.api.rate_limit import ReadRateLimit
from app.schemas.reconciliation import BalanceMismatchRead, ReconciliationRead

router = APIRouter(prefix="/reconciliation", tags=["ledger"])


@router.get(
    "",
    dependencies=[ReadRateLimit],
    summary="Compare account balances with ledger-derived balances",
    description=(
        "Read-only check over one consistent snapshot: every account balance must equal the "
        "sum of its ledger entries, all entries must sum to zero, and every transfer must "
        "have exactly two balanced entries."
    ),
)
def reconcile(service: ReconciliationServiceDep) -> ReconciliationRead:
    report = service.run()
    return ReconciliationRead(
        ok=report.ok,
        accounts_checked=report.accounts_checked,
        ledger_entries_checked=report.ledger_entries_checked,
        ledger_total_minor=report.ledger_total_minor,
        balance_total_minor=report.balance_total_minor,
        balance_mismatches=[
            BalanceMismatchRead(
                account_id=m.account_id,
                balance_minor=m.balance_minor,
                ledger_balance_minor=m.ledger_balance_minor,
            )
            for m in report.balance_mismatches
        ],
        unbalanced_transfer_ids=report.unbalanced_transfer_ids,
    )
