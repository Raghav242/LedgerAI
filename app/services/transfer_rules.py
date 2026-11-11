"""Pure money-movement validation rules.

These functions touch no database or HTTP objects, so they can be unit-tested with
plain in-memory Account instances. `validate_against_accounts` must only ever be called
with accounts that are row-locked, otherwise the balance it checks may already be stale.
"""

import uuid

from app.core.exceptions import (
    CurrencyMismatchError,
    InsufficientFundsError,
    InvalidAmountError,
    SameAccountTransferError,
    SystemAccountNotAllowedError,
)
from app.models import Account, TransferKind


def validate_transfer_request(
    source_account_id: uuid.UUID, destination_account_id: uuid.UUID, amount_minor: int
) -> None:
    """Checks that need no account data; run before touching the database."""
    if amount_minor <= 0:
        raise InvalidAmountError(
            "Transfer amount must be a positive integer number of minor units",
            {"amount_minor": amount_minor},
        )
    if source_account_id == destination_account_id:
        raise SameAccountTransferError(
            "Source and destination accounts must be different",
            {"account_id": str(source_account_id)},
        )


def validate_account_kinds(kind: TransferKind, source: Account, destination: Account) -> None:
    """Merchant transfers may not touch the system account; funding must come from it."""
    if destination.is_system:
        raise SystemAccountNotAllowedError(
            "System accounts cannot receive funds", {"account_id": str(destination.id)}
        )
    if kind == TransferKind.TRANSFER and source.is_system:
        raise SystemAccountNotAllowedError(
            "System accounts cannot be used in merchant transfers",
            {"account_id": str(source.id)},
        )
    if kind == TransferKind.DEV_FUNDING and not source.is_system:
        raise SystemAccountNotAllowedError(
            "Funding must come from the system funding account", {"account_id": str(source.id)}
        )


def validate_transfer_against_accounts(
    source: Account, destination: Account, amount_minor: int, currency: str
) -> None:
    """Checks that depend on the current (locked) state of both accounts."""
    if source.currency != destination.currency or source.currency != currency:
        raise CurrencyMismatchError(
            "Transfer currency must match both accounts' currency",
            {
                "transfer_currency": currency,
                "source_currency": source.currency,
                "destination_currency": destination.currency,
            },
        )
    # The system issuance account has no funds limit: its balance is minus all money issued.
    if not source.is_system and source.balance_minor < amount_minor:
        raise InsufficientFundsError(
            "Source account has insufficient funds",
            {
                "account_id": str(source.id),
                "balance_minor": source.balance_minor,
                "requested_minor": amount_minor,
            },
        )
