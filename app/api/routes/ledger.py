import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import InsightsServiceDep, LimitQuery, OffsetQuery
from app.api.rate_limit import ReadRateLimit
from app.schemas.common import Page
from app.schemas.ledger import LedgerEntryRead

router = APIRouter(prefix="/ledger", tags=["ledger"])


@router.get(
    "/entries",
    summary="List ledger postings (newest first, read-only)",
    description=(
        "Immutable double-entry postings. Debits are negative, credits positive; the two "
        "legs of one transfer are returned next to each other (debit first)."
    ),
    dependencies=[ReadRateLimit],
)
def list_ledger_entries(
    service: InsightsServiceDep,
    account_id: Annotated[
        uuid.UUID | None, Query(description="Only postings on this account")
    ] = None,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
) -> Page[LedgerEntryRead]:
    entries = service.list_ledger_entries(account_id=account_id, limit=limit, offset=offset)
    return Page[LedgerEntryRead](
        items=[LedgerEntryRead.model_validate(e) for e in entries], limit=limit, offset=offset
    )
