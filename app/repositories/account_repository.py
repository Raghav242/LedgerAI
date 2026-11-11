import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account, AccountKind


class AccountRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, account: Account) -> Account:
        self._session.add(account)
        # Flush (not commit) so DB defaults/constraints are applied inside the caller's transaction.
        self._session.flush()
        return account

    def get(self, account_id: uuid.UUID) -> Account | None:
        """Unlocked read. Never use the result to decide a balance change."""
        return self._session.get(Account, account_id)

    def lock_for_update(self, account_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, Account]:
        """Row-lock the given accounts (SELECT ... FOR UPDATE) in ascending ID order.

        Locks are taken one statement at a time in a globally consistent order, so two
        transactions touching the same pair of accounts always queue in the same order
        and cannot deadlock each other (A->B and B->A both lock min(A,B) first).

        populate_existing=True overwrites any copy already in the session's identity map
        with the row as read *under the lock*; without it a stale, pre-lock balance
        could be reused. Missing IDs are simply absent from the result.
        """
        locked: dict[uuid.UUID, Account] = {}
        for account_id in sorted(set(account_ids)):
            stmt = (
                select(Account)
                .where(Account.id == account_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            account = self._session.scalars(stmt).one_or_none()
            if account is not None:
                locked[account_id] = account
        return locked

    def list_merchants(self, *, limit: int, offset: int) -> list[Account]:
        stmt = (
            select(Account)
            .where(Account.kind == AccountKind.MERCHANT)
            .order_by(Account.created_at, Account.id)
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.scalars(stmt))
