from typing import Annotated, Any

from pydantic import BaseModel, Field

# Upper bound per request ($1B in cents). Keeps every intermediate sum far below
# PostgreSQL's BIGINT limit (~9.2e18) and rejects absurd inputs early.
MAX_AMOUNT_MINOR = 100_000_000_000

# strict=True: reject 10.5, "1000" and true. Money amounts must be real JSON integers.
AmountMinor = Annotated[
    int,
    Field(
        strict=True,
        gt=0,
        le=MAX_AMOUNT_MINOR,
        description="Amount in integer minor units (e.g. cents). 1050 = 10.50 USD.",
        examples=[1050],
    ),
]


class Page[ItemT](BaseModel):
    items: list[ItemT]
    limit: int
    offset: int


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any = None


class ErrorResponse(BaseModel):
    error: ErrorBody
