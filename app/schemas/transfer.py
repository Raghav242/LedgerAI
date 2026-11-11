import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models import Currency, TransferKind, TransferStatus
from app.schemas.common import AmountMinor


class TransferCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_account_id: uuid.UUID
    destination_account_id: uuid.UUID
    amount_minor: AmountMinor
    # Required, not defaulted: the client states which currency it intends to move.
    currency: Currency


class TransferRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_account_id: uuid.UUID
    destination_account_id: uuid.UUID
    amount_minor: int
    currency: Currency
    status: TransferStatus
    kind: TransferKind
    created_at: datetime
