import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.dependencies import LimitQuery, OffsetQuery, TransferServiceDep
from app.schemas.common import ErrorResponse, Page
from app.schemas.transfer import TransferCreate, TransferRead

router = APIRouter(prefix="/transfers", tags=["transfers"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Transfer funds between two accounts",
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
        status.HTTP_409_CONFLICT: {"model": ErrorResponse},
    },
)
def create_transfer(payload: TransferCreate, service: TransferServiceDep) -> TransferRead:
    transfer = service.create_transfer(
        source_account_id=payload.source_account_id,
        destination_account_id=payload.destination_account_id,
        amount_minor=payload.amount_minor,
        currency=payload.currency,
    )
    return TransferRead.model_validate(transfer)


@router.get("", summary="List transfer history (newest first)")
def list_transfers(
    service: TransferServiceDep,
    account_id: Annotated[
        uuid.UUID | None,
        Query(description="Only transfers where this account is source or destination"),
    ] = None,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
) -> Page[TransferRead]:
    transfers = service.list_transfers(account_id=account_id, limit=limit, offset=offset)
    return Page[TransferRead](
        items=[TransferRead.model_validate(t) for t in transfers], limit=limit, offset=offset
    )
