import uuid

from sqlalchemy.orm import Session

from app.core.exceptions import AccountNotFoundError
from app.models import Account, Transfer, TransferKind, TransferStatus
from app.repositories.account_repository import AccountRepository
from app.repositories.ledger_repository import LedgerRepository
from app.repositories.transfer_repository import TransferRepository
from app.services.transfer_rules import (
    validate_account_kinds,
    validate_transfer_against_accounts,
    validate_transfer_request,
)


class LedgerPoster:
    """Applies one balanced money movement inside the caller's open transaction.

    Shared by merchant transfers and dev funding so that every movement of money goes
    through the same lock -> validate -> mutate -> record sequence. It never commits:
    the calling service owns the transaction boundary.
    """

    def __init__(self, session: Session) -> None:
        self._accounts = AccountRepository(session)
        self._transfers = TransferRepository(session)
        self._ledger = LedgerRepository(session)

    def post(
        self,
        *,
        source_account_id: uuid.UUID,
        destination_account_id: uuid.UUID,
        amount_minor: int,
        currency: str,
        kind: TransferKind,
    ) -> Transfer:
        validate_transfer_request(source_account_id, destination_account_id, amount_minor)

        # 1. Lock both rows (deterministic order) BEFORE reading balances.
        locked = self._accounts.lock_for_update([source_account_id, destination_account_id])
        source = self._require(locked, source_account_id, role="source")
        destination = self._require(locked, destination_account_id, role="destination")

        # 2. Validate against the locked, current state.
        validate_account_kinds(kind, source, destination)
        validate_transfer_against_accounts(source, destination, amount_minor, currency)

        # 3. Mutate. Safe as read-modify-write because no one else can change these rows
        #    until this transaction ends.
        source.balance_minor -= amount_minor
        destination.balance_minor += amount_minor

        # 4. Record the movement and its two ledger postings.
        transfer = self._transfers.add(
            Transfer(
                source_account_id=source.id,
                destination_account_id=destination.id,
                amount_minor=amount_minor,
                currency=currency,
                status=TransferStatus.COMPLETED,
                kind=kind,
            )
        )
        self._ledger.add_postings(transfer)
        return transfer

    @staticmethod
    def _require(locked: dict[uuid.UUID, Account], account_id: uuid.UUID, *, role: str) -> Account:
        account = locked.get(account_id)
        if account is None:
            raise AccountNotFoundError(
                f"{role.capitalize()} account not found",
                {"account_id": str(account_id), "role": role},
            )
        return account
