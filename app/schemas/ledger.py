import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models import Currency, LedgerEntryRole


class LedgerEntryRead(BaseModel):
    """One immutable ledger posting. Debits are negative, credits positive."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    transfer_id: uuid.UUID
    account_id: uuid.UUID
    role: LedgerEntryRole
    amount_minor: int
    currency: Currency
    created_at: datetime
