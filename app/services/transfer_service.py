import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.database import LOCK_NOT_AVAILABLE, atomic, sqlstate_of
from app.core.exceptions import (
    BusinessRuleError,
    IdempotencyKeyReusedError,
    IdempotencyRequestInProgressError,
)
from app.models import IdempotencyOutcome, IdempotencyRecord, Transfer, TransferKind
from app.repositories.idempotency_repository import IdempotencyRepository
from app.repositories.transfer_repository import TransferRepository
from app.schemas.common import ErrorBody
from app.schemas.transfer import TransferRead
from app.services.idempotency import request_fingerprint
from app.services.ledger_posting import LedgerPoster

logger = logging.getLogger(__name__)

CREATE_TRANSFER_OPERATION = "transfers.create"


@dataclass(frozen=True, slots=True)
class TransferCommand:
    source_account_id: uuid.UUID
    destination_account_id: uuid.UUID
    amount_minor: int
    currency: str

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "source_account_id": str(self.source_account_id),
            "destination_account_id": str(self.destination_account_id),
            "amount_minor": self.amount_minor,
            "currency": str(self.currency),
        }


@dataclass(frozen=True, slots=True)
class TransferResult:
    """The stored, replayable outcome of one idempotent transfer request.

    `response_body` is the exact JSON returned to the client the first time: a
    TransferRead on success, an {"error": ...} envelope for a business rejection.
    """

    outcome: IdempotencyOutcome
    response_body: dict[str, Any]
    error_code: str | None
    replayed: bool
    transfer_id: uuid.UUID | None

    @property
    def succeeded(self) -> bool:
        return self.outcome == IdempotencyOutcome.SUCCEEDED


class TransferService:
    def __init__(self, session: Session, *, lock_timeout_ms: int) -> None:
        self._session = session
        self._lock_timeout_ms = lock_timeout_ms
        self._poster = LedgerPoster(session)
        self._idempotency = IdempotencyRepository(session)
        self._transfers = TransferRepository(session)

    def create_transfer(
        self, *, scope: str, idempotency_key: str, command: TransferCommand
    ) -> TransferResult:
        """Execute a transfer at most once per (scope, idempotency_key).

        Single transaction, claim-and-complete:
          1. INSERT the idempotency claim (waits if a same-key request is in flight).
          2. Lock both accounts in ID order, validate, update balances, insert the
             transfer and its two ledger entries.
          3. Store the response snapshot on the claim row.
          4. COMMIT once: money movement and idempotency outcome become durable together.

        Business rejections (insufficient funds, unknown account, ...) are rolled back to a
        SAVEPOINT taken after the claim, then stored as the key's final outcome, so a retry
        replays the same error. Any other failure rolls back everything, including the
        claim, so the same key can safely be retried.
        """
        fingerprint = request_fingerprint(CREATE_TRANSFER_OPERATION, command.canonical_payload())

        with atomic(self._session, lock_timeout_ms=self._lock_timeout_ms):
            record = self._claim(scope, idempotency_key, fingerprint)
            if record is None:
                result = self._replay(scope, idempotency_key, fingerprint)
            else:
                result = self._execute(record, command)

        # Logged only after COMMIT: no I/O beyond the database while row locks are held.
        if result.replayed:
            logger.info("Transfer replayed key=%s outcome=%s", idempotency_key, result.outcome)
        elif result.succeeded:
            logger.info(
                "Transfer completed id=%s source=%s destination=%s amount_minor=%d key=%s",
                result.transfer_id,
                command.source_account_id,
                command.destination_account_id,
                command.amount_minor,
                idempotency_key,
            )
        else:
            logger.info(
                "Transfer rejected code=%s source=%s destination=%s amount_minor=%d key=%s",
                result.error_code,
                command.source_account_id,
                command.destination_account_id,
                command.amount_minor,
                idempotency_key,
            )
        return result

    def list_transfers(
        self,
        *,
        account_id: uuid.UUID | None,
        kind: TransferKind | None,
        limit: int,
        offset: int,
    ) -> list[Transfer]:
        return self._transfers.list_all(
            account_id=account_id, kind=kind, limit=limit, offset=offset
        )

    # --- steps ------------------------------------------------------------------------

    def _claim(self, scope: str, key: str, fingerprint: str) -> IdempotencyRecord | None:
        try:
            return self._idempotency.try_claim(
                scope=scope, idempotency_key=key, request_fingerprint=fingerprint
            )
        except OperationalError as exc:
            if sqlstate_of(exc) == LOCK_NOT_AVAILABLE:
                # Another request with this key is still running and held the unique-index
                # lock longer than lock_timeout. It will either commit (retry -> replay) or
                # roll back (retry -> execute), so retrying the same key is always safe.
                raise IdempotencyRequestInProgressError(
                    "A request with this Idempotency-Key is still being processed; "
                    "retry later with the same key",
                    {"idempotency_key": key},
                ) from exc
            raise

    def _replay(self, scope: str, key: str, fingerprint: str) -> TransferResult:
        record = self._idempotency.get(scope=scope, idempotency_key=key)
        if record is None:  # pragma: no cover - rows are never deleted in this phase
            raise IdempotencyRequestInProgressError(
                "Idempotency record changed concurrently; retry with the same key",
                {"idempotency_key": key},
            )
        if record.request_fingerprint != fingerprint:
            raise IdempotencyKeyReusedError(
                "This Idempotency-Key was already used with different request parameters",
                {"idempotency_key": key},
            )
        if record.outcome is None or record.response_body is None:  # pragma: no cover
            # Unreachable by construction: claim and completion commit together.
            raise RuntimeError(f"Committed idempotency record {record.id} has no outcome")
        return TransferResult(
            outcome=IdempotencyOutcome(record.outcome),
            response_body=record.response_body,
            error_code=record.error_code,
            replayed=True,
            transfer_id=record.transfer_id,
        )

    def _execute(self, record: IdempotencyRecord, command: TransferCommand) -> TransferResult:
        try:
            with self._session.begin_nested():  # SAVEPOINT
                transfer = self._poster.post(
                    source_account_id=command.source_account_id,
                    destination_account_id=command.destination_account_id,
                    amount_minor=command.amount_minor,
                    currency=command.currency,
                    kind=TransferKind.TRANSFER,
                )
        except BusinessRuleError as exc:
            # ROLLBACK TO SAVEPOINT already happened: no balance or ledger change survives,
            # but the claim row does, so the rejection becomes this key's final outcome.
            body = {
                "error": ErrorBody(
                    code=exc.code, message=exc.message, details=exc.details or None
                ).model_dump(mode="json")
            }
            self._idempotency.complete(
                record,
                outcome=IdempotencyOutcome.REJECTED,
                response_body=body,
                error_code=exc.code,
            )
            return TransferResult(
                outcome=IdempotencyOutcome.REJECTED,
                response_body=body,
                error_code=exc.code,
                replayed=False,
                transfer_id=None,
            )

        body = TransferRead.model_validate(transfer).model_dump(mode="json")
        self._idempotency.complete(
            record,
            outcome=IdempotencyOutcome.SUCCEEDED,
            response_body=body,
            transfer_id=transfer.id,
        )
        return TransferResult(
            outcome=IdempotencyOutcome.SUCCEEDED,
            response_body=body,
            error_code=None,
            replayed=False,
            transfer_id=transfer.id,
        )
