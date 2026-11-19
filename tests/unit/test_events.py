import json
import random
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import OperationalError

from app.core.exceptions import ConcurrencyConflictError
from app.events.backoff import backoff_ms
from app.events.consumer import PermanentEventError, is_transient
from app.events.ids import event_id_for
from app.events.rabbitmq import Topology
from app.events.schemas import (
    ACCOUNT_FUNDED,
    TRANSFER_COMPLETED,
    AccountFundedV1,
    InvalidEventError,
    TransferCompletedV1,
    parse_envelope,
)
from app.models import Transfer, TransferKind
from app.repositories.outbox_repository import build_event

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


def _transfer(kind: TransferKind) -> Transfer:
    return Transfer(
        id=uuid.uuid4(),
        source_account_id=uuid.uuid4(),
        destination_account_id=uuid.uuid4(),
        amount_minor=1_250,
        currency="USD",
        status="completed",
        kind=kind,
        created_at=NOW,
    )


def _envelope(**overrides: object) -> dict:
    transfer = _transfer(TransferKind.TRANSFER)
    event = build_event(transfer, correlation_id="req-1")
    assert event is not None
    body = {
        "event_id": str(event.id),
        "event_type": event.event_type,
        "event_version": event.event_version,
        "aggregate_id": str(event.aggregate_id),
        "occurred_at": NOW.isoformat(),
        "correlation_id": "req-1",
        "payload": event.payload,
    }
    body.update(overrides)
    return body


def test_event_ids_are_deterministic_per_operation() -> None:
    operation = uuid.uuid4()
    assert event_id_for(TRANSFER_COMPLETED, operation) == event_id_for(
        TRANSFER_COMPLETED, operation
    )
    assert event_id_for(TRANSFER_COMPLETED, operation) != event_id_for(ACCOUNT_FUNDED, operation)
    assert event_id_for(TRANSFER_COMPLETED, operation) != event_id_for(
        TRANSFER_COMPLETED, uuid.uuid4()
    )


def test_transfer_builds_a_versioned_transfer_completed_event() -> None:
    transfer = _transfer(TransferKind.TRANSFER)
    event = build_event(transfer, correlation_id="req-9")
    assert event is not None
    assert event.id == event_id_for(TRANSFER_COMPLETED, transfer.id)
    assert (event.event_type, event.event_version, event.aggregate_id) == (
        TRANSFER_COMPLETED,
        1,
        transfer.id,
    )
    assert TransferCompletedV1.model_validate(event.payload).amount_minor == 1_250
    assert event.correlation_id == "req-9"


def test_funding_builds_an_account_funded_event_keyed_by_account() -> None:
    transfer = _transfer(TransferKind.DEV_FUNDING)
    event = build_event(transfer, correlation_id=None)
    assert event is not None
    assert event.event_type == ACCOUNT_FUNDED
    assert event.aggregate_id == transfer.destination_account_id
    assert (
        AccountFundedV1.model_validate(event.payload).account_id == transfer.destination_account_id
    )


def test_legacy_opening_balances_emit_no_event() -> None:
    assert build_event(_transfer(TransferKind.LEGACY_OPENING_BALANCE), correlation_id=None) is None


def test_valid_envelope_parses_with_typed_payload() -> None:
    envelope, payload = parse_envelope(json.dumps(_envelope()).encode())
    assert envelope.event_type == TRANSFER_COMPLETED
    assert isinstance(payload, TransferCompletedV1)


def test_unknown_fields_are_tolerated_within_a_version() -> None:
    body = _envelope()
    body["payload"]["new_optional_field"] = "x"
    body["producer"] = "api"
    parse_envelope(json.dumps(body).encode())


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"\xff\xfe",
        json.dumps({"event_type": TRANSFER_COMPLETED}).encode(),  # missing envelope fields
        json.dumps(_envelope(event_version=2)).encode(),  # unsupported version
        json.dumps(_envelope(event_type="refund.created")).encode(),  # unknown type
        json.dumps(_envelope(payload={"transfer_id": "nope"})).encode(),  # bad payload
        json.dumps(
            _envelope(payload={**_envelope()["payload"], "amount_minor": 12.5})
        ).encode(),  # non-integer money
    ],
    ids=["not-json", "not-utf8", "missing-fields", "v2", "unknown-type", "bad-payload", "float"],
)
def test_malformed_or_unsupported_events_are_rejected(body: bytes) -> None:
    with pytest.raises(InvalidEventError):
        parse_envelope(body)


def test_backoff_grows_exponentially_with_jitter_and_cap() -> None:
    rng = random.Random(7)
    for attempt, ceiling in [(1, 500), (2, 1_000), (3, 2_000), (10, 60_000), (40, 60_000)]:
        values = [backoff_ms(attempt, base_ms=500, cap_ms=60_000, rng=rng) for _ in range(200)]
        assert all(ceiling / 2 <= v <= ceiling for v in values)
        assert len(set(values)) > 50  # jittered, not lockstep
    with pytest.raises(ValueError):
        backoff_ms(0, base_ms=1, cap_ms=1)


@pytest.mark.parametrize(
    ("error", "transient"),
    [
        (OperationalError("SELECT", {}, Exception("connection refused")), True),
        (ConcurrencyConflictError("lock timeout"), True),
        (RuntimeError("unexpected"), True),  # unknown -> bounded retries, then DLQ
        (PermanentEventError("bad"), False),
        (InvalidEventError("bad"), False),
    ],
)
def test_failure_classification(error: Exception, transient: bool) -> None:
    assert is_transient(error) is transient


def test_topology_names_are_namespaced() -> None:
    topology = Topology("t1", "audit", (10, 20), 3)
    assert topology.exchange == "t1.events"
    assert topology.queue == "t1.audit"
    assert topology.dead_letter_queue == "t1.audit.dlq"
    assert topology.all_queues == [
        "t1.audit",
        "t1.audit.dlq",
        "t1.audit.retry.1",
        "t1.audit.retry.2",
    ]
