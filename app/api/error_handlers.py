import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import (
    AccountNotFoundError,
    CommitOutcomeUnknownError,
    ConcurrencyConflictError,
    CurrencyMismatchError,
    DomainError,
    IdempotencyKeyReusedError,
    IdempotencyRequestInProgressError,
    InsufficientFundsError,
    InvalidAmountError,
    RateLimiterUnavailableError,
    RateLimitExceededError,
    SameAccountTransferError,
    SystemAccountNotAllowedError,
)
from app.schemas.common import ErrorBody, ErrorResponse

logger = logging.getLogger(__name__)

DOMAIN_ERROR_STATUS: dict[type[DomainError], int] = {
    AccountNotFoundError: status.HTTP_404_NOT_FOUND,
    InvalidAmountError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    SameAccountTransferError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    CurrencyMismatchError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    SystemAccountNotAllowedError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    # 409: the request is well-formed but conflicts with the account's current state.
    InsufficientFundsError: status.HTTP_409_CONFLICT,
    # Same key, different body: the key cannot mean two different requests.
    IdempotencyKeyReusedError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    # Same key still being processed by another request: retry later, same key.
    IdempotencyRequestInProgressError: status.HTTP_409_CONFLICT,
    # Rolled back because of lock contention: retry, same key.
    ConcurrencyConflictError: status.HTTP_503_SERVICE_UNAVAILABLE,
    # May or may not have committed: retry with the SAME key to find out.
    CommitOutcomeUnknownError: status.HTTP_500_INTERNAL_SERVER_ERROR,
    # Quota used up; nothing executed. Retry after Retry-After seconds.
    RateLimitExceededError: status.HTTP_429_TOO_MANY_REQUESTS,
    # Writes fail closed while the shared limiter is unreachable; nothing executed.
    RateLimiterUnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
}
_STATUS_BY_CODE = {cls.code: code for cls, code in DOMAIN_ERROR_STATUS.items()}
RETRYABLE_ERRORS = (
    ConcurrencyConflictError,
    IdempotencyRequestInProgressError,
    RateLimiterUnavailableError,
)
RETRY_AFTER_SECONDS = "1"


def _error_response(
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(code=code, message=message, details=details))
    return JSONResponse(
        status_code=status_code, content=body.model_dump(mode="json"), headers=headers
    )


def _status_for(exc: DomainError) -> int:
    for cls in type(exc).__mro__:
        if cls in DOMAIN_ERROR_STATUS:
            return DOMAIN_ERROR_STATUS[cls]
    return status.HTTP_400_BAD_REQUEST


def status_for_error_code(code: str) -> int:
    """HTTP status for a stored error code (used when replaying an idempotent rejection)."""
    return _STATUS_BY_CODE.get(code, status.HTTP_400_BAD_REQUEST)


async def handle_domain_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DomainError)
    headers = {"Retry-After": RETRY_AFTER_SECONDS} if isinstance(exc, RETRYABLE_ERRORS) else None
    if isinstance(exc, RateLimitExceededError):
        headers = {"Retry-After": str(exc.retry_after_seconds)}
    if isinstance(
        exc, ConcurrencyConflictError | CommitOutcomeUnknownError | RateLimiterUnavailableError
    ):
        logger.warning("%s: %s", exc.code, exc.__cause__ or exc.message)
    return _error_response(
        _status_for(exc), exc.code, exc.message, exc.details or None, headers=headers
    )


async def handle_validation_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    details = [
        {"loc": list(err["loc"]), "message": err["msg"], "type": err["type"]}
        for err in exc.errors()
    ]
    return _error_response(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "VALIDATION_ERROR",
        "Request validation failed",
        details,
    )


async def handle_http_exception(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = HTTPStatus(exc.status_code).name
    return _error_response(exc.status_code, code, str(exc.detail))


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    # Full traceback goes to the log; the client only gets a generic message.
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return _error_response(
        status.HTTP_500_INTERNAL_SERVER_ERROR, "INTERNAL_ERROR", "An unexpected error occurred"
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, handle_domain_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(Exception, handle_unexpected_error)
