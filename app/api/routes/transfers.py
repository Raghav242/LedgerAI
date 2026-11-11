import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status
from fastapi.responses import JSONResponse

from app.api.dependencies import (
    IdempotencyKeyHeader,
    LimitQuery,
    OffsetQuery,
    PrincipalDep,
    TransferServiceDep,
)
from app.api.error_handlers import status_for_error_code
from app.models import TransferKind
from app.schemas.common import ErrorResponse, Page
from app.schemas.transfer import TransferCreate, TransferRead
from app.services.transfer_service import TransferCommand, TransferResult

router = APIRouter(prefix="/transfers", tags=["transfers"])

REPLAY_HEADER = "Idempotent-Replayed"


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=TransferRead,
    summary="Transfer funds between two accounts (idempotent)",
    description=(
        "Requires an `Idempotency-Key` header. Retrying with the same key and body returns "
        "the original response (with `Idempotent-Replayed: true`) and never moves money "
        "twice. Reusing a key with a different body returns 422. On 409 "
        "IDEMPOTENCY_REQUEST_IN_PROGRESS, 503 CONCURRENCY_CONFLICT or 500 OUTCOME_UNKNOWN, "
        "retry with the SAME key."
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
        status.HTTP_409_CONFLICT: {"model": ErrorResponse},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
    },
)
def create_transfer(
    payload: TransferCreate,
    idempotency_key: IdempotencyKeyHeader,
    principal: PrincipalDep,
    service: TransferServiceDep,
) -> JSONResponse:
    result = service.create_transfer(
        scope=principal.scope,
        idempotency_key=idempotency_key,
        command=TransferCommand(
            source_account_id=payload.source_account_id,
            destination_account_id=payload.destination_account_id,
            amount_minor=payload.amount_minor,
            currency=payload.currency,
        ),
    )
    return _to_response(result)


def _to_response(result: TransferResult) -> JSONResponse:
    if result.succeeded:
        status_code = status.HTTP_201_CREATED
    else:
        status_code = status_for_error_code(result.error_code or "")
    headers = {REPLAY_HEADER: "true"} if result.replayed else None
    return JSONResponse(content=result.response_body, status_code=status_code, headers=headers)


@router.get("", summary="List transfer history (newest first)")
def list_transfers(
    service: TransferServiceDep,
    account_id: Annotated[
        uuid.UUID | None,
        Query(description="Only transfers where this account is source or destination"),
    ] = None,
    kind: Annotated[
        TransferKind | None,
        Query(description="Only this kind of movement (transfer, dev_funding, ...)"),
    ] = None,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
) -> Page[TransferRead]:
    transfers = service.list_transfers(account_id=account_id, kind=kind, limit=limit, offset=offset)
    return Page[TransferRead](
        items=[TransferRead.model_validate(t) for t in transfers], limit=limit, offset=offset
    )
