import datetime as dt

from pydantic import BaseModel, Field

from app.models import TransferKind


class TransferKindSummary(BaseModel):
    kind: TransferKind
    count: int
    volume_minor: int = Field(description="Sum of amount_minor for this kind")


class DailyTransferActivity(BaseModel):
    date: dt.date = Field(description="UTC calendar day")
    count: int
    volume_minor: int


class SystemSummaryRead(BaseModel):
    """Read-only aggregates computed over one consistent database snapshot."""

    merchant_account_count: int
    merchant_balance_total_minor: int = Field(
        description="Simulated money currently held by merchant accounts"
    )
    ledger_entry_count: int
    transfers_by_kind: list[TransferKindSummary] = Field(
        description="One row per transfer kind, including kinds with no transfers"
    )
    daily_transfer_activity: list[DailyTransferActivity] = Field(
        description="Merchant-to-merchant transfers per UTC day, oldest first, gaps filled"
    )
    generated_at: dt.datetime
