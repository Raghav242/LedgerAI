import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.account import AccountCreate, AccountFund
from app.schemas.common import MAX_AMOUNT_MINOR
from app.schemas.transfer import TransferCreate


def transfer_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source_account_id": str(uuid.uuid4()),
        "destination_account_id": str(uuid.uuid4()),
        "amount_minor": 1_000,
        "currency": "USD",
    }
    payload.update(overrides)
    return payload


def test_transfer_create_accepts_valid_payload() -> None:
    transfer = TransferCreate.model_validate(transfer_payload())
    assert transfer.amount_minor == 1_000


@pytest.mark.parametrize(
    "amount",
    [0, -5, 10.5, 100.0, "100", True, None, MAX_AMOUNT_MINOR + 1],
    ids=["zero", "negative", "fraction", "float", "string", "bool", "null", "too-large"],
)
def test_transfer_create_rejects_invalid_amounts(amount: Any) -> None:
    with pytest.raises(ValidationError):
        TransferCreate.model_validate(transfer_payload(amount_minor=amount))


@pytest.mark.parametrize("currency", ["EUR", "usd", "", None])
def test_transfer_create_rejects_unsupported_currency(currency: Any) -> None:
    with pytest.raises(ValidationError):
        TransferCreate.model_validate(transfer_payload(currency=currency))


def test_transfer_create_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        TransferCreate.model_validate(transfer_payload(status="completed"))


def test_transfer_create_rejects_malformed_uuid() -> None:
    with pytest.raises(ValidationError):
        TransferCreate.model_validate(transfer_payload(source_account_id="not-a-uuid"))


def test_account_create_strips_name_and_defaults_currency() -> None:
    account = AccountCreate.model_validate({"name": "  Acme Store  "})
    assert account.name == "Acme Store"
    assert account.currency == "USD"


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
def test_account_create_rejects_bad_names(name: str) -> None:
    with pytest.raises(ValidationError):
        AccountCreate.model_validate({"name": name})


def test_account_create_rejects_client_supplied_balance() -> None:
    with pytest.raises(ValidationError):
        AccountCreate.model_validate({"name": "Acme", "balance_minor": 1_000_000})


@pytest.mark.parametrize("amount", [0, -1, 1.5])
def test_account_fund_rejects_invalid_amounts(amount: Any) -> None:
    with pytest.raises(ValidationError):
        AccountFund.model_validate({"amount_minor": amount})
