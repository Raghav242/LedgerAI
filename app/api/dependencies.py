from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header, Query, Request
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.principal import DEMO_PRINCIPAL, Principal
from app.services.account_service import AccountService
from app.services.reconciliation_service import ReconciliationService
from app.services.transfer_service import TransferService


def get_session(request: Request) -> Iterator[Session]:
    """One session per request; every repository in the request shares it.

    The `with` block closes the session deterministically when the request finishes;
    closing also rolls back anything a service left uncommitted.
    """
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        yield session


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_current_principal() -> Principal:
    """No authentication yet: every caller is the fixed demo merchant (see core/principal.py)."""
    return DEMO_PRINCIPAL


SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
PrincipalDep = Annotated[Principal, Depends(get_current_principal)]


def get_account_service(session: SessionDep, settings: SettingsDep) -> AccountService:
    return AccountService(session, lock_timeout_ms=settings.transfer_lock_timeout_ms)


def get_transfer_service(session: SessionDep, settings: SettingsDep) -> TransferService:
    return TransferService(session, lock_timeout_ms=settings.transfer_lock_timeout_ms)


def get_reconciliation_service(session: SessionDep) -> ReconciliationService:
    return ReconciliationService(session)


AccountServiceDep = Annotated[AccountService, Depends(get_account_service)]
TransferServiceDep = Annotated[TransferService, Depends(get_transfer_service)]
ReconciliationServiceDep = Annotated[ReconciliationService, Depends(get_reconciliation_service)]

LimitQuery = Annotated[int, Query(ge=1, le=100, description="Maximum items to return")]
OffsetQuery = Annotated[int, Query(ge=0, description="Number of items to skip")]

IdempotencyKeyHeader = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=255,
        pattern=r"^[\x21-\x7E]+$",  # visible ASCII, no spaces
        description=(
            "Client-generated unique key (a UUID is recommended). Retrying with the same key "
            "and body returns the original response instead of moving money again."
        ),
    ),
]
