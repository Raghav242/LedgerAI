"""Pure business-rule tests: no database, no HTTP."""

import uuid

import pytest

from app.core.exceptions import (
    CurrencyMismatchError,
    InsufficientFundsError,
    InvalidAmountError,
    SameAccountTransferError,
)
from app.models import Account
from app.services.transfer_rules import (
    validate_transfer_against_accounts,
    validate_transfer_request,
)


def make_account(balance_minor: int = 0, currency: str = "USD") -> Account:
    return Account(id=uuid.uuid4(), name="test", currency=currency, balance_minor=balance_minor)


@pytest.mark.parametrize("amount", [0, -1, -10_000])
def test_rejects_non_positive_amount(amount: int) -> None:
    with pytest.raises(InvalidAmountError):
        validate_transfer_request(uuid.uuid4(), uuid.uuid4(), amount)


def test_rejects_same_source_and_destination() -> None:
    account_id = uuid.uuid4()
    with pytest.raises(SameAccountTransferError):
        validate_transfer_request(account_id, account_id, 100)


def test_accepts_valid_request() -> None:
    validate_transfer_request(uuid.uuid4(), uuid.uuid4(), 1)


def test_rejects_insufficient_funds() -> None:
    with pytest.raises(InsufficientFundsError) as exc_info:
        validate_transfer_against_accounts(make_account(99), make_account(), 100, "USD")
    assert exc_info.value.details["balance_minor"] == 99


def test_allows_transferring_exact_balance() -> None:
    validate_transfer_against_accounts(make_account(100), make_account(), 100, "USD")


def test_rejects_mismatched_account_currencies() -> None:
    # Only USD can be stored today; an in-memory EUR account exercises the rule anyway.
    with pytest.raises(CurrencyMismatchError):
        validate_transfer_against_accounts(
            make_account(1_000), make_account(currency="EUR"), 100, "USD"
        )


def test_rejects_transfer_currency_different_from_accounts() -> None:
    with pytest.raises(CurrencyMismatchError):
        validate_transfer_against_accounts(make_account(1_000), make_account(), 100, "EUR")
