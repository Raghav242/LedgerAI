import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.models import AccountKind, Currency
from app.schemas.common import AmountMinor

AccountName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class AccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: AccountName
    currency: Currency = Currency.USD


class AccountFund(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount_minor: AmountMinor


class AccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    currency: Currency
    kind: AccountKind
    balance_minor: int
    created_at: datetime


class AccountBalance(BaseModel):
    account_id: uuid.UUID
    currency: Currency
    balance_minor: int
