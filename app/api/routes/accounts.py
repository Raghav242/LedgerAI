import uuid

from fastapi import APIRouter, status

from app.api.dependencies import AccountServiceDep, LimitQuery, OffsetQuery
from app.api.rate_limit import ReadRateLimit, WriteRateLimit
from app.schemas.account import AccountBalance, AccountCreate, AccountFund, AccountRead
from app.schemas.common import ErrorResponse, Page

router = APIRouter(prefix="/accounts", tags=["accounts"])

NOT_FOUND = {status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}}


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create a virtual account",
    dependencies=[WriteRateLimit],
)
def create_account(payload: AccountCreate, service: AccountServiceDep) -> AccountRead:
    account = service.create_account(payload.name, payload.currency)
    return AccountRead.model_validate(account)


@router.get("", summary="List accounts", dependencies=[ReadRateLimit])
def list_accounts(
    service: AccountServiceDep, limit: LimitQuery = 50, offset: OffsetQuery = 0
) -> Page[AccountRead]:
    accounts = service.list_accounts(limit=limit, offset=offset)
    return Page[AccountRead](
        items=[AccountRead.model_validate(a) for a in accounts], limit=limit, offset=offset
    )


@router.get(
    "/{account_id}", responses=NOT_FOUND, summary="Get an account", dependencies=[ReadRateLimit]
)
def get_account(account_id: uuid.UUID, service: AccountServiceDep) -> AccountRead:
    return AccountRead.model_validate(service.get_account(account_id))


@router.get(
    "/{account_id}/balance",
    responses=NOT_FOUND,
    summary="Get an account balance",
    dependencies=[ReadRateLimit],
)
def get_account_balance(account_id: uuid.UUID, service: AccountServiceDep) -> AccountBalance:
    account = service.get_account(account_id)
    return AccountBalance(
        account_id=account.id, currency=account.currency, balance_minor=account.balance_minor
    )


# Registered only when ENABLE_DEV_ENDPOINTS=true (see app/api/router.py).
dev_router = APIRouter(prefix="/accounts", tags=["development only"])


@dev_router.post(
    "/{account_id}/fund",
    dependencies=[WriteRateLimit],
    responses=NOT_FOUND,
    summary="[DEV ONLY] Add simulated funds to an account",
    description=(
        "**Development-only.** Mints simulated money out of thin air. This route only "
        "exists when `ENABLE_DEV_ENDPOINTS=true` and is refused at startup when "
        "`APP_ENV=production`. Never expose it unrestricted in a public deployment."
    ),
)
def fund_account(
    account_id: uuid.UUID, payload: AccountFund, service: AccountServiceDep
) -> AccountRead:
    return AccountRead.model_validate(service.fund_account(account_id, payload.amount_minor))
