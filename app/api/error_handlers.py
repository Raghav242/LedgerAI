import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import (
    AccountNotFoundError,
    CurrencyMismatchError,
    DomainError,
    InsufficientFundsError,
    InvalidAmountError,
    SameAccountTransferError,
)
from app.schemas.common import ErrorBody, ErrorResponse

logger = logging.getLogger(__name__)

DOMAIN_ERROR_STATUS: dict[type[DomainError], int] = {
    AccountNotFoundError: status.HTTP_404_NOT_FOUND,
    InvalidAmountError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    SameAccountTransferError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    CurrencyMismatchError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    # 409: the request is well-formed but conflicts with the account's current state.
    InsufficientFundsError: status.HTTP_409_CONFLICT,
}


def _error_response(status_code: int, code: str, message: str, details: Any = None) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(code=code, message=message, details=details))
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


def _status_for(exc: DomainError) -> int:
    for cls in type(exc).__mro__:
        if cls in DOMAIN_ERROR_STATUS:
            return DOMAIN_ERROR_STATUS[cls]
    return status.HTTP_400_BAD_REQUEST


async def handle_domain_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DomainError)
    return _error_response(_status_for(exc), exc.code, exc.message, exc.details or None)


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
