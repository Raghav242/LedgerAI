"""The event contract: a versioned envelope plus one payload schema per (type, version).

Producers and consumers evolve independently, so versions are explicit. A consumer
rejects (dead-letters) any event type/version it does not understand instead of guessing.
Unknown extra fields inside a known version are ignored, so producers can add optional
fields without a version bump.
"""

import json
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

TRANSFER_COMPLETED = "transfer.completed"
ACCOUNT_FUNDED = "account.funded"


class InvalidEventError(Exception):
    """Malformed or unsupported event: retrying can never succeed."""


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class TransferCompletedV1(_Payload):
    transfer_id: uuid.UUID
    source_account_id: uuid.UUID
    destination_account_id: uuid.UUID
    amount_minor: int = Field(strict=True, gt=0)
    currency: str = Field(min_length=3, max_length=3)
    occurred_at: datetime


class AccountFundedV1(_Payload):
    transfer_id: uuid.UUID
    account_id: uuid.UUID
    amount_minor: int = Field(strict=True, gt=0)
    currency: str = Field(min_length=3, max_length=3)
    occurred_at: datetime


PAYLOAD_SCHEMAS: dict[tuple[str, int], type[_Payload]] = {
    (TRANSFER_COMPLETED, 1): TransferCompletedV1,
    (ACCOUNT_FUNDED, 1): AccountFundedV1,
}


class EventEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    event_id: uuid.UUID
    event_type: str
    event_version: int = Field(strict=True, ge=1)
    aggregate_id: uuid.UUID
    occurred_at: datetime
    correlation_id: str | None = None
    payload: dict[str, Any]

    def to_json(self) -> bytes:
        return self.model_dump_json().encode()

    def typed_payload(self) -> _Payload:
        schema = PAYLOAD_SCHEMAS[(self.event_type, self.event_version)]
        return schema.model_validate(self.payload)


def parse_envelope(body: bytes) -> tuple[EventEnvelope, _Payload]:
    """Decode and fully validate a message body, or raise InvalidEventError."""
    try:
        raw = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidEventError(f"body is not JSON: {exc}") from exc
    try:
        envelope = EventEnvelope.model_validate(raw)
    except ValidationError as exc:
        raise InvalidEventError(f"invalid envelope: {exc.errors(include_url=False)}") from exc
    schema = PAYLOAD_SCHEMAS.get((envelope.event_type, envelope.event_version))
    if schema is None:
        raise InvalidEventError(
            f"unsupported event {envelope.event_type!r} version {envelope.event_version}"
        )
    try:
        payload = schema.model_validate(envelope.payload)
    except ValidationError as exc:
        raise InvalidEventError(f"invalid payload: {exc.errors(include_url=False)}") from exc
    return envelope, payload
