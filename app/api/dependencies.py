from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Query, Request
from sqlalchemy.orm import Session

from app.services.account_service import AccountService
from app.services.transfer_service import TransferService


def get_session(request: Request) -> Iterator[Session]:
    """One session per request.

    The `with` block closes the session deterministically when the request finishes;
    closing also rolls back anything a service left uncommitted.
    """
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def get_account_service(session: SessionDep) -> AccountService:
    return AccountService(session)


def get_transfer_service(session: SessionDep) -> TransferService:
    return TransferService(session)


AccountServiceDep = Annotated[AccountService, Depends(get_account_service)]
TransferServiceDep = Annotated[TransferService, Depends(get_transfer_service)]

LimitQuery = Annotated[int, Query(ge=1, le=100, description="Maximum items to return")]
OffsetQuery = Annotated[int, Query(ge=0, description="Number of items to skip")]
