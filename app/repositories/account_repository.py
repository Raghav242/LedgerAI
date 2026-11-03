import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account


class AccountRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, account: Account) -> Account:
        self._session.add(account)
        # Flush (not commit) so DB defaults/constraints are applied inside the caller's transaction.
        self._session.flush()
        return account

    def get(self, account_id: uuid.UUID) -> Account | None:
        # Phase 2 will add a locking variant (SELECT ... FOR UPDATE) for transfers.
        return self._session.get(Account, account_id)

    def list_all(self, *, limit: int, offset: int) -> list[Account]:
        stmt = select(Account).order_by(Account.created_at, Account.id).limit(limit).offset(offset)
        return list(self._session.scalars(stmt))
