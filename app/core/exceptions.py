"""Errors raised below the API layer.

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


# --- Business-rule rejections --------------------------------------------------------
# Deterministic answers about the request itself. For idempotent transfers they are a
# *final* outcome: stored under the Idempotency-Key and replayed on retry.


class BusinessRuleError(DomainError):
    code = "BUSINESS_RULE_VIOLATION"


class AccountNotFoundError(BusinessRuleError):
    code = "ACCOUNT_NOT_FOUND"


class InvalidAmountError(BusinessRuleError):
    code = "INVALID_AMOUNT"


class SameAccountTransferError(BusinessRuleError):
    code = "SAME_ACCOUNT_TRANSFER"


class CurrencyMismatchError(BusinessRuleError):
    code = "CURRENCY_MISMATCH"


class InsufficientFundsError(BusinessRuleError):
    code = "INSUFFICIENT_FUNDS"


class SystemAccountNotAllowedError(BusinessRuleError):
    code = "SYSTEM_ACCOUNT_NOT_ALLOWED"


# --- Idempotency protocol errors -----------------------------------------------------
# Never stored: they describe how the key was used, not the transfer's outcome.


class IdempotencyKeyReusedError(DomainError):
    code = "IDEMPOTENCY_KEY_REUSED"


class IdempotencyRequestInProgressError(DomainError):
    code = "IDEMPOTENCY_REQUEST_IN_PROGRESS"


# --- Transient / infrastructure outcomes ---------------------------------------------
# Nothing was committed (or, for OutcomeUnknown, we cannot tell). Clients should retry
# with the SAME Idempotency-Key, never a new one.


class ConcurrencyConflictError(DomainError):
    """Lock timeout, deadlock or serialization failure. The transaction was rolled back."""

    code = "CONCURRENCY_CONFLICT"


class CommitOutcomeUnknownError(DomainError):
    """The connection failed while COMMIT was in flight: it may or may not have committed."""

    code = "OUTCOME_UNKNOWN"
