import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import IdempotencyOutcome, IdempotencyRecord


class IdempotencyRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def try_claim(
        self, *, scope: str, idempotency_key: str, request_fingerprint: str
    ) -> IdempotencyRecord | None:
        """Insert a claim row, or return None if (scope, key) already exists.

        INSERT ... ON CONFLICT DO NOTHING relies on the unique index for mutual exclusion:
        if another transaction has inserted the same key but not committed yet, this
        statement WAITS for it (bounded by lock_timeout). If that transaction commits we
        get the conflict (None); if it rolls back, our insert proceeds and we own the key.
        """
        stmt = (
            insert(IdempotencyRecord)
            .values(
                id=uuid.uuid4(),
                scope=scope,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
            .on_conflict_do_nothing(index_elements=["scope", "idempotency_key"])
            .returning(IdempotencyRecord.id)
        )
        record_id = self._session.scalar(stmt)
        if record_id is None:
            return None
        return self._session.get_one(IdempotencyRecord, record_id)

    def get(self, *, scope: str, idempotency_key: str) -> IdempotencyRecord | None:
        stmt = (
            select(IdempotencyRecord)
            .where(
                IdempotencyRecord.scope == scope,
                IdempotencyRecord.idempotency_key == idempotency_key,
            )
            .execution_options(populate_existing=True)
        )
        return self._session.scalars(stmt).one_or_none()

    def complete(
        self,
        record: IdempotencyRecord,
        *,
        outcome: IdempotencyOutcome,
        response_body: dict[str, Any],
        transfer_id: uuid.UUID | None = None,
        error_code: str | None = None,
    ) -> None:
        record.outcome = outcome
        record.response_body = response_body
        record.transfer_id = transfer_id
        record.error_code = error_code
        record.completed_at = func.now()
        self._session.flush()
