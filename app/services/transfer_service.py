import logging
import uuid

from sqlalchemy.orm import Session

from app.core.database import atomic
from app.core.exceptions import AccountNotFoundError, DomainError
from app.models import Account, Transfer, TransferStatus
from app.repositories.account_repository import AccountRepository
from app.repositories.transfer_repository import TransferRepository
from app.services.transfer_rules import (
    validate_transfer_against_accounts,
    validate_transfer_request,
)

logger = logging.getLogger(__name__)


class TransferService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._accounts = AccountRepository(session)
        self._transfers = TransferRepository(session)

    def create_transfer(
        self,
        *,
        source_account_id: uuid.UUID,
        destination_account_id: uuid.UUID,
        amount_minor: int,
        currency: str,
    ) -> Transfer:
        """Move money between two accounts in a single database transaction.

        Debit, credit and the transfer record either all commit or all roll back.

        NOT concurrency-safe in Phase 1: balances are read without row locks, so two
        concurrent transfers from the same account can both pass the funds check.
        The CHECK (balance_minor >= 0) constraint stops the balance going negative,
        but lost updates are still possible. Phase 2 addresses this.
        """
        try:
            validate_transfer_request(source_account_id, destination_account_id, amount_minor)
            with atomic(self._session):
                source = self._load_account(source_account_id, role="source")
                destination = self._load_account(destination_account_id, role="destination")
                validate_transfer_against_accounts(source, destination, amount_minor, currency)

                source.balance_minor -= amount_minor
                destination.balance_minor += amount_minor
                transfer = self._transfers.add(
                    Transfer(
                        source_account_id=source.id,
                        destination_account_id=destination.id,
                        amount_minor=amount_minor,
                        currency=currency,
                        status=TransferStatus.COMPLETED,
                    )
                )
        except DomainError as exc:
            logger.info(
                "Transfer rejected code=%s source=%s destination=%s amount_minor=%d",
                exc.code,
                source_account_id,
                destination_account_id,
                amount_minor,
            )
            raise

        logger.info(
            "Transfer completed id=%s source=%s destination=%s amount_minor=%d currency=%s",
            transfer.id,
            transfer.source_account_id,
            transfer.destination_account_id,
            transfer.amount_minor,
            transfer.currency,
        )
        return transfer

    def list_transfers(
        self, *, account_id: uuid.UUID | None, limit: int, offset: int
    ) -> list[Transfer]:
        return self._transfers.list_all(account_id=account_id, limit=limit, offset=offset)

    def _load_account(self, account_id: uuid.UUID, *, role: str) -> Account:
        account = self._accounts.get(account_id)
        if account is None:
            raise AccountNotFoundError(
                f"{role.capitalize()} account not found",
                {"account_id": str(account_id), "role": role},
            )
        return account
