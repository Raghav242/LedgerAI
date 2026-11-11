from enum import StrEnum


class Currency(StrEnum):
    USD = "USD"


class AccountKind(StrEnum):
    MERCHANT = "merchant"
    # Internal issuance account that demo money is minted from. Its balance is the
    # negative of all money issued, so the sum of every account balance is always zero.
    SYSTEM = "system"


class TransferStatus(StrEnum):
    # Only completed movements are persisted. Later phases (async processing, fraud
    # review) will add states such as "pending" and "rejected".
    COMPLETED = "completed"


class TransferKind(StrEnum):
    TRANSFER = "transfer"  # merchant -> merchant, via POST /api/v1/transfers
    DEV_FUNDING = "dev_funding"  # system funding account -> merchant (dev endpoint)
    # Created once by migration 0002 for Phase 1 balances that had no ledger history.
    LEGACY_OPENING_BALANCE = "legacy_opening_balance"


class LedgerEntryRole(StrEnum):
    DEBIT = "debit"  # money leaving the account: negative amount
    CREDIT = "credit"  # money entering the account: positive amount


class IdempotencyOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
