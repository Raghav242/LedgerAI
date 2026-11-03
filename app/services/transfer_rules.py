"""Pure transfer validation rules.

These functions touch no database or HTTP objects, so they can be unit-tested with
plain in-memory Account instances.
"""

import uuid

from app.core.exceptions import (
    CurrencyMismatchError,
    InsufficientFundsError,
    InvalidAmountError,
    SameAccountTransferError,
)
from app.models import Account


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


def validate_transfer_against_accounts(
    source: Account, destination: Account, amount_minor: int, currency: str
) -> None:
    """Checks that depend on the current state of both accounts."""
    if source.currency != destination.currency or source.currency != currency:
        raise CurrencyMismatchError(
            "Transfer currency must match both accounts' currency",
            {
                "transfer_currency": currency,
                "source_currency": source.currency,
                "destination_currency": destination.currency,
            },
        )
    if source.balance_minor < amount_minor:
        raise InsufficientFundsError(
            "Source account has insufficient funds",
            {
                "account_id": str(source.id),
                "balance_minor": source.balance_minor,
                "requested_minor": amount_minor,
            },
        )
