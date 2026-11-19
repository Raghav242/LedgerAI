import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.events.ids import event_id_for
from app.events.schemas import ACCOUNT_FUNDED, TRANSFER_COMPLETED, EventEnvelope
from app.models import OutboxEvent, Transfer, TransferKind

EVENT_VERSION = 1


def build_event(transfer: Transfer, *, correlation_id: str | None) -> OutboxEvent | None:
    """The outbox event describing a just-posted money movement (None if none is due)."""
    occurred_at = transfer.created_at.isoformat()
    if transfer.kind == TransferKind.TRANSFER:
        event_type = TRANSFER_COMPLETED
        aggregate_id = transfer.id
        payload: dict[str, Any] = {
            "transfer_id": str(transfer.id),
            "source_account_id": str(transfer.source_account_id),
            "destination_account_id": str(transfer.destination_account_id),
            "amount_minor": transfer.amount_minor,
            "currency": transfer.currency,
            "occurred_at": occurred_at,
        }
    elif transfer.kind == TransferKind.DEV_FUNDING:
        event_type = ACCOUNT_FUNDED
        aggregate_id = transfer.destination_account_id
        payload = {
            "transfer_id": str(transfer.id),
            "account_id": str(transfer.destination_account_id),
            "amount_minor": transfer.amount_minor,
            "currency": transfer.currency,
            "occurred_at": occurred_at,
        }
    else:
        return None
    return OutboxEvent(
        id=event_id_for(event_type, transfer.id),
        aggregate_id=aggregate_id,
        event_type=event_type,
        event_version=EVENT_VERSION,
        payload=payload,
        correlation_id=correlation_id,
    )


@dataclass(frozen=True, slots=True)
class ClaimedEvent:
    id: uuid.UUID
    event_type: str
    event_version: int
    aggregate_id: uuid.UUID
    payload: dict[str, Any]
    correlation_id: str | None
    created_at: datetime
    attempts: int

    def envelope(self) -> EventEnvelope:
        return EventEnvelope(
            event_id=self.id,
            event_type=self.event_type,
            event_version=self.event_version,
            aggregate_id=self.aggregate_id,
            occurred_at=self.created_at,
            correlation_id=self.correlation_id,
            payload=self.payload,
        )


# One statement: pick claimable rows without waiting for rows another publisher holds
# (SKIP LOCKED), and mark them processing under a fresh lease + fencing token. A row is
# claimable if pending and due, OR processing with an expired lease (abandoned claim).
_CLAIM_SQL = text(
    """
    WITH candidates AS (
        SELECT id FROM outbox_events
        WHERE status <> 'published'
          AND (
                (status = 'pending' AND available_at <= now())
             OR (status = 'processing' AND claimed_until < now())
          )
        ORDER BY available_at, created_at
        LIMIT :batch_size
        FOR UPDATE SKIP LOCKED
    )
    UPDATE outbox_events AS o
    SET status = 'processing',
        claimed_until = now() + make_interval(secs => :lease_seconds),
        claim_token = :token,
        attempts = o.attempts + 1
    FROM candidates
    WHERE o.id = candidates.id
    RETURNING o.id, o.event_type, o.event_version, o.aggregate_id, o.payload,
              o.correlation_id, o.created_at, o.attempts
    """
)

_MARK_PUBLISHED_SQL = text(
    """
    UPDATE outbox_events
    SET status = 'published', published_at = now(), claimed_until = NULL,
        claim_token = NULL, last_error = NULL
    WHERE id = ANY(:ids) AND claim_token = :token
    """
)

# Back to pending with exponential backoff + equal jitter computed in SQL from the
# row's own attempt count: delay = cap(base * 2^(attempts-1)) * (0.5 + random()/2).
_RELEASE_SQL = text(
    """
    UPDATE outbox_events
    SET status = 'pending', claimed_until = NULL, claim_token = NULL, last_error = :error,
        available_at = now() + make_interval(secs =>
            least(:cap_ms, :base_ms * power(2, least(attempts - 1, 32)))
            * (0.5 + random() / 2) / 1000.0)
    WHERE id = ANY(:ids) AND claim_token = :token
    """
)


class OutboxRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, event: OutboxEvent) -> OutboxEvent:
        """Insert inside the caller's transaction (never commits)."""
        self._session.add(event)
        self._session.flush()
        return event

    def claim_batch(
        self, *, batch_size: int, lease_seconds: int, token: uuid.UUID
    ) -> list[ClaimedEvent]:
        rows = self._session.execute(
            _CLAIM_SQL, {"batch_size": batch_size, "lease_seconds": lease_seconds, "token": token}
        ).mappings()
        claimed = [ClaimedEvent(**row) for row in rows]
        return sorted(claimed, key=lambda e: e.created_at)

    def mark_published(self, ids: list[uuid.UUID], *, token: uuid.UUID) -> int:
        """Only rows still held under `token` change: a publisher whose lease expired
        and whose events were re-claimed by someone else cannot overwrite that claim."""
        if not ids:
            return 0
        result = self._session.execute(_MARK_PUBLISHED_SQL, {"ids": ids, "token": token})
        return result.rowcount

    def release_for_retry(
        self, ids: list[uuid.UUID], *, token: uuid.UUID, error: str, base_ms: int, cap_ms: int
    ) -> int:
        if not ids:
            return 0
        result = self._session.execute(
            _RELEASE_SQL,
            {
                "ids": ids,
                "token": token,
                "error": error[:2000],
                "base_ms": base_ms,
                "cap_ms": cap_ms,
            },
        )
        return result.rowcount
