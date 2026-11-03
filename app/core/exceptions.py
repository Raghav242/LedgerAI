"""Domain errors raised by the service layer.

They carry a stable machine-readable `code` but no HTTP status: mapping errors to
HTTP responses is the API layer's job (see app/api/error_handlers.py).
"""

from typing import Any


class DomainError(Exception):
    code = "DOMAIN_ERROR"

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class AccountNotFoundError(DomainError):
    code = "ACCOUNT_NOT_FOUND"


class InvalidAmountError(DomainError):
    code = "INVALID_AMOUNT"


class SameAccountTransferError(DomainError):
    code = "SAME_ACCOUNT_TRANSFER"


class CurrencyMismatchError(DomainError):
    code = "CURRENCY_MISMATCH"


class InsufficientFundsError(DomainError):
    code = "INSUFFICIENT_FUNDS"
