import logging
import uuid

from sqlalchemy.orm import Session

from app.core.database import atomic
from app.core.exceptions import AccountNotFoundError, InvalidAmountError
from app.models import Account, Currency
from app.repositories.account_repository import AccountRepository

logger = logging.getLogger(__name__)


class AccountService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._accounts = AccountRepository(session)

    def create_account(self, name: str, currency: Currency) -> Account:
        with atomic(self._session):
            account = self._accounts.add(Account(name=name, currency=currency, balance_minor=0))
        logger.info("Account created id=%s currency=%s", account.id, account.currency)
        return account

    def get_account(self, account_id: uuid.UUID) -> Account:
        account = self._accounts.get(account_id)
        if account is None:
            raise AccountNotFoundError("Account not found", {"account_id": str(account_id)})
        return account

    def list_accounts(self, *, limit: int, offset: int) -> list[Account]:
        return self._accounts.list_all(limit=limit, offset=offset)

    def fund_account(self, account_id: uuid.UUID, amount_minor: int) -> Account:
        """DEVELOPMENT ONLY: mint simulated money into an account.

        Not concurrency-safe in Phase 1: two simultaneous fundings read the same balance
        and the later commit overwrites the earlier one (lost update).
        """
        if amount_minor <= 0:
            raise InvalidAmountError(
                "Funding amount must be a positive integer number of minor units",
                {"amount_minor": amount_minor},
            )
        with atomic(self._session):
            account = self.get_account(account_id)
            account.balance_minor += amount_minor
        logger.info(
            "DEV funding applied account_id=%s amount_minor=%d new_balance_minor=%d",
            account.id,
            amount_minor,
            account.balance_minor,
        )
        return account
