"""Tracing is observation only: with OpenTelemetry ON, financial behaviour is unchanged and
the trace id / correlation id stay consistent from the HTTP request through the outbox,
the publisher and the consumer (real PostgreSQL; real RabbitMQ for the async half).

Spans go to an in-memory exporter installed as the process-wide tracer provider.
"""

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.events.consumer import Outcome
from app.events.ids import event_id_for
from app.events.schemas import TRANSFER_COMPLETED
from app.main import create_app
from app.observability import tracing
from tests.integration.conftest import create_account, fund_account, get_balance, post_transfer
from tests.integration.messaging import drain, make_consumer, make_publisher

EXPORTER = InMemorySpanExporter()


@pytest.fixture(scope="module", autouse=True)
def _tracer_provider() -> Iterator[None]:
    current = trace.get_tracer_provider()
    if not isinstance(current, TracerProvider):
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
        trace.set_tracer_provider(provider)
    else:  # already installed by an earlier module in this process
        current.add_span_processor(SimpleSpanProcessor(EXPORTER))
    tracing._configured = True  # configure_tracing() must not replace our provider
    yield


@pytest.fixture
def traced_client(test_settings: Settings, engine: Engine) -> Iterator[TestClient]:
    EXPORTER.clear()
    app = create_app(test_settings.model_copy(update={"otel_enabled": True}))
    with TestClient(app) as client:
        yield client


def test_traced_transfer_moves_money_exactly_like_an_untraced_one(
    traced_client: TestClient, engine: Engine
) -> None:
    a = create_account(traced_client, "Traced A")["id"]
    b = create_account(traced_client, "Traced B")["id"]
    fund_account(traced_client, a, 10_000)
    key = str(uuid.uuid4())

    first = post_transfer(traced_client, a, b, 2_500, idempotency_key=key)
    replay = post_transfer(traced_client, a, b, 2_500, idempotency_key=key)
    rejected = post_transfer(traced_client, a, b, 1_000_000)

    assert first.status_code == 201 and replay.status_code == 201
    assert replay.headers["idempotent-replayed"] == "true"
    assert replay.json() == first.json()
    assert rejected.json()["error"]["code"] == "INSUFFICIENT_FUNDS"
    assert (get_balance(traced_client, a), get_balance(traced_client, b)) == (7_500, 2_500)
    assert traced_client.get("/api/v1/reconciliation").json()["ok"] is True

    # The response names its trace; the outbox row carries the same trace context.
    trace_id = first.headers["x-trace-id"]
    assert len(trace_id) == 32 and replay.headers["x-trace-id"] != trace_id
    event_id = event_id_for(TRANSFER_COMPLETED, uuid.UUID(first.json()["id"]))
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT trace_parent FROM outbox_events WHERE id = :id"), {"id": event_id}
        ).one()
    assert tracing.trace_id_of(row.trace_parent) == trace_id

    names = {
        s.name
        for s in EXPORTER.get_finished_spans()
        if format(s.context.trace_id, "032x") == trace_id
    }
    assert {
        "rate_limit.check",
        "transfer.create",
        "idempotency.claim",
        "ledger.post",
        "outbox.write",
    } <= names


@pytest.mark.rabbitmq
def test_trace_and_correlation_ids_survive_the_broker(
    traced_client: TestClient,
    event_settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    a = create_account(traced_client, "Async A")["id"]
    b = create_account(traced_client, "Async B")["id"]
    fund_account(traced_client, a, 10_000)
    response = post_transfer(traced_client, a, b, 1_200)
    trace_id = response.headers["x-trace-id"]
    request_id = "req-from-nginx"  # the API stores X-Request-ID as correlation id
    event_id = event_id_for(TRANSFER_COMPLETED, uuid.UUID(response.json()["id"]))
    with session_factory() as session, session.begin():
        session.execute(
            text("UPDATE outbox_events SET correlation_id = :c WHERE id = :id"),
            {"c": request_id, "id": event_id},
        )

    publisher = make_publisher(event_settings, session_factory)
    assert publisher.publish_once().published == 2
    publisher.close()
    consumer = make_consumer(event_settings, session_factory)
    assert drain(consumer, event_settings) == [Outcome.PROCESSED, Outcome.PROCESSED]
    consumer.close()

    spans = [
        s for s in EXPORTER.get_finished_spans() if format(s.context.trace_id, "032x") == trace_id
    ]
    by_name = {s.name: s for s in spans}
    publish = by_name["outbox.publish"]
    consume = by_name["event.consume ledger-audit"]
    assert publish.attributes["messaging.message.id"] == str(event_id)
    assert consume.parent is not None and consume.parent.span_id == publish.context.span_id
    with session_factory() as session:
        audit = session.execute(
            text("SELECT correlation_id FROM event_audit_log WHERE event_id = :id"),
            {"id": event_id},
        ).one()
    assert audit.correlation_id == request_id
