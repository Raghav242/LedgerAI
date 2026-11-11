import logging
import uuid

from sqlalchemy.orm import Session

from app.core.database import atomic
from app.core.exceptions import AccountNotFoundError, InvalidAmountError
from app.models import SYSTEM_FUNDING_ACCOUNT_ID, Account, Currency, TransferKind
from app.repositories.account_repository import AccountRepository
from app.services.ledger_posting import LedgerPoster

logger = logging.getLogger(__name__)


class AccountService:
    def __init__(self, session: Session, *, lock_timeout_ms: int) -> None:
        self._session = session
        self._lock_timeout_ms = lock_timeout_ms
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
        return self._accounts.list_merchants(limit=limit, offset=offset)

    def fund_account(self, account_id: uuid.UUID, amount_minor: int) -> Account:
        """DEVELOPMENT ONLY: move simulated money from the system funding account.

        A normal double-entry movement (debit system account, credit merchant) under the
        same row locks as a transfer, so concurrent fundings cannot lose updates. Every
        funding locks the single system account row, so fundings are serialised with each
        other; that is acceptable for a dev-only endpoint.
        """
        if amount_minor <= 0:
            raise InvalidAmountError(
                "Funding amount must be a positive integer number of minor units",
                {"amount_minor": amount_minor},
            )
        with atomic(self._session, lock_timeout_ms=self._lock_timeout_ms):
            transfer = LedgerPoster(self._session).post(
                source_account_id=SYSTEM_FUNDING_ACCOUNT_ID,
                destination_account_id=account_id,
                amount_minor=amount_minor,
                currency=Currency.USD,  # the only supported currency
                kind=TransferKind.DEV_FUNDING,
            )
            account = self._accounts.get(account_id)  # identity map: the locked, updated row
        assert account is not None
        logger.info(
            "DEV funding applied transfer_id=%s account_id=%s amount_minor=%d new_balance_minor=%d",
            transfer.id,
            account.id,
            amount_minor,
            account.balance_minor,
        )
        return account
