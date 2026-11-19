from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import InsightsServiceDep
from app.api.rate_limit import ReadRateLimit
from app.schemas.stats import DailyTransferActivity, SystemSummaryRead, TransferKindSummary
from app.services.insights_service import MAX_ACTIVITY_DAYS

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get(
    "/summary",
    summary="System-wide aggregates for dashboards (read-only)",
    description=(
        "Account, transfer and ledger totals plus per-day transfer activity, all computed "
        "in the database over one consistent snapshot, so a client never has to download "
        "the full history to show totals."
    ),
    dependencies=[ReadRateLimit],
)
def get_summary(
    service: InsightsServiceDep,
    activity_days: Annotated[
        int, Query(ge=1, le=MAX_ACTIVITY_DAYS, description="Days of daily activity to return")
    ] = 14,
) -> SystemSummaryRead:
    summary = service.summary(activity_days=activity_days)
    return SystemSummaryRead(
        merchant_account_count=summary.merchant_account_count,
        merchant_balance_total_minor=summary.merchant_balance_total_minor,
        ledger_entry_count=summary.ledger_entry_count,
        transfers_by_kind=[
            TransferKindSummary(kind=k.kind, count=k.count, volume_minor=k.volume_minor)
            for k in summary.transfers_by_kind
        ],
        daily_transfer_activity=[
            DailyTransferActivity(date=d.day, count=d.count, volume_minor=d.volume_minor)
            for d in summary.daily_transfer_activity
        ],
        generated_at=summary.generated_at,
    )
