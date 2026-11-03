from enum import StrEnum


class Currency(StrEnum):
    USD = "USD"


class TransferStatus(StrEnum):
    # Phase 1 only persists transfers that succeeded. Later phases (async processing,
    # fraud review) will add states such as "pending" and "rejected".
    COMPLETED = "completed"
