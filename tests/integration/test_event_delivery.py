"""At-least-once delivery, exactly-once effects: real RabbitMQ + real PostgreSQL."""

import json
import threading
import uuid

import pika
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.events.consumer import ERROR_HEADER, AuditHandler, Outcome
from app.events.ids import event_id_for
from app.events.rabbitmq import connect, declare_topology, persistent_properties
from app.events.schemas import TRANSFER_COMPLETED
from app.models import EventAuditEntry, OutboxEvent, ProcessedEvent
from app.repositories.outbox_repository import OutboxRepository
from tests.integration.messaging import (
    SimulatedCrash,
    declare,
    drain,
    get_messages,
    make_consumer,
    make_publisher,
    queue_depth,
    topology_of,
    wait_for,
)
from tests.integration.support import create_account, reconcile, transfer

pytestmark = pytest.mark.rabbitmq

Factory = sessionmaker[Session]


def _count(factory: Factory, model, event_id: uuid.UUID | None = None) -> int:  # type: ignore[no-untyped-def]
    with factory() as session:
        stmt = select(func.count()).select_from(model)
        if event_id is not None:
            stmt = stmt.where(model.event_id == event_id)
        return session.scalar(stmt) or 0


def _statuses(factory: Factory) -> dict[str, int]:
    with factory() as session:
        rows = session.execute(text("SELECT status, count(*) FROM outbox_events GROUP BY 1"))
        return dict(rows.all())


def _one_transfer(factory: Factory) -> uuid.UUID:
    """A committed transfer; returns its event id. (Funding also emits one event.)"""
    a = create_account(factory, funded=10_000)
    b = create_account(factory)
    result = transfer(factory, a, b, 700)
    assert result.transfer_id is not None
    return event_id_for(TRANSFER_COMPLETED, result.transfer_id)


def _raw_publish(settings: Settings, body: bytes, routing_key: str = TRANSFER_COMPLETED) -> None:
    connection = connect(settings)
    channel = connection.channel()
    channel.confirm_delivery()
    channel.basic_publish(
        exchange=topology_of(settings).exchange,
        routing_key=routing_key,
        body=body,
        properties=persistent_properties(
            message_id=str(uuid.uuid4()), event_type=routing_key, correlation_id=None
        ),
        mandatory=True,
    )
    connection.close()


# --- happy path & duplicates ----------------------------------------------------------------


def test_committed_event_is_published_consumed_and_acked(
    event_settings: Settings, session_factory: Factory
) -> None:
    event_id = _one_transfer(session_factory)

    report = make_publisher(event_settings, session_factory).publish_once()
    assert (report.claimed, report.published, report.failed_count) == (2, 2, 0)
    assert _statuses(session_factory) == {"published": 2}

    consumer = make_consumer(event_settings, session_factory)
    assert drain(consumer, event_settings) == [Outcome.PROCESSED, Outcome.PROCESSED]
    consumer.close()

    assert _count(session_factory, EventAuditEntry, event_id) == 1
    assert _count(session_factory, ProcessedEvent, event_id) == 1
    # Acked: nothing comes back after the consumer's channel is closed.
    assert queue_depth(event_settings, topology_of(event_settings).queue) == 0
    assert reconcile(session_factory).ok


def test_duplicate_publications_have_a_single_durable_effect(
    event_settings: Settings, session_factory: Factory
) -> None:
    event_id = _one_transfer(session_factory)
    publisher = make_publisher(event_settings, session_factory)
    publisher.publish_once()
    for _ in range(2):  # force re-publication of the same rows (what a lost confirm does)
        with session_factory() as session, session.begin():
            session.execute(text("UPDATE outbox_events SET status = 'pending'"))
        assert publisher.publish_once().published == 2
    assert queue_depth(event_settings, topology_of(event_settings).queue) == 6

    consumer = make_consumer(event_settings, session_factory)
    outcomes = drain(consumer, event_settings)
    consumer.close()

    assert outcomes.count(Outcome.PROCESSED) == 2  # 2 distinct events
    assert outcomes.count(Outcome.DUPLICATE) == 4
    assert _count(session_factory, EventAuditEntry, event_id) == 1
    assert _count(session_factory, ProcessedEvent, event_id) == 1


def test_crash_after_broker_confirm_before_outbox_update_republishes_safely(
    event_settings: Settings, session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_id = _one_transfer(session_factory)
    publisher = make_publisher(event_settings, session_factory)

    def crash(*_args: object, **_kwargs: object) -> int:
        raise SimulatedCrash("publisher died after the broker confirmed")

    with monkeypatch.context() as patch:
        patch.setattr(OutboxRepository, "mark_published", crash)
        with pytest.raises(SimulatedCrash):
            publisher.publish_once()
    publisher.close()
    # Broker has the messages; the outbox still says "processing" (claim pending expiry).
    assert queue_depth(event_settings, topology_of(event_settings).queue) == 2
    assert _statuses(session_factory) == {"processing": 2}

    with session_factory() as session, session.begin():  # lease expires
        session.execute(text("UPDATE outbox_events SET claimed_until = now() - interval '1 s'"))
    restarted = make_publisher(event_settings, session_factory)
    assert restarted.publish_once().published == 2  # re-published: at-least-once
    assert _statuses(session_factory) == {"published": 2}
    assert queue_depth(event_settings, topology_of(event_settings).queue) == 4

    consumer = make_consumer(event_settings, session_factory)
    outcomes = drain(consumer, event_settings)
    consumer.close()
    assert sorted(outcomes) == sorted([Outcome.PROCESSED] * 2 + [Outcome.DUPLICATE] * 2)
    assert _count(session_factory, EventAuditEntry, event_id) == 1


def test_unroutable_events_are_retried_later_not_lost(
    event_settings: Settings, session_factory: Factory
) -> None:
    with session_factory() as session, session.begin():
        session.add(
            OutboxEvent(
                id=uuid.uuid4(),
                aggregate_id=uuid.uuid4(),
                event_type="nobody.listens",  # no queue is bound for this routing key
                event_version=1,
                payload={},
            )
        )
    report = make_publisher(event_settings, session_factory).publish_once()

    assert report.published == 0 and report.failed_count == 1
    with session_factory() as session:
        row = session.scalars(select(OutboxEvent)).one()
    assert row.status == "pending" and "unroutable" in (row.last_error or "")


# --- consumer crashes -------------------------------------------------------------------------


def _first_delivery(consumer):  # type: ignore[no-untyped-def]
    topology_queue = consumer._topology.queue
    method, properties, body = consumer.channel.basic_get(topology_queue)
    assert method is not None
    return method, properties, body


def test_worker_crash_before_db_commit_reprocesses_once(
    event_settings: Settings, session_factory: Factory
) -> None:
    event_id = _one_transfer(session_factory)
    make_publisher(event_settings, session_factory).publish_once()

    def crash_on_transfer(event, redelivered):  # type: ignore[no-untyped-def]
        if event.event_id == event_id:
            raise SimulatedCrash("died inside the transaction")

    doomed = make_consumer(event_settings, session_factory, before_commit=crash_on_transfer)
    delivered = None
    for _ in range(2):  # the funding event first, then the transfer event
        method, properties, body = _first_delivery(doomed)
        try:
            doomed.handle_delivery(doomed.channel, method, properties, body)
        except SimulatedCrash:
            delivered = method
            break
    assert delivered is not None
    doomed.close()  # connection dies without an ack -> broker requeues the message

    assert _count(session_factory, ProcessedEvent, event_id) == 0  # rolled back
    assert _count(session_factory, EventAuditEntry, event_id) == 0

    survivor = make_consumer(event_settings, session_factory)
    assert drain(survivor, event_settings) == [Outcome.PROCESSED]
    survivor.close()
    assert _count(session_factory, EventAuditEntry, event_id) == 1
    assert queue_depth(event_settings, topology_of(event_settings).queue) == 0


def test_worker_crash_after_commit_before_ack_is_deduplicated(
    event_settings: Settings, session_factory: Factory
) -> None:
    event_id = _one_transfer(session_factory)
    make_publisher(event_settings, session_factory).publish_once()

    def crash_on_transfer(event, redelivered):  # type: ignore[no-untyped-def]
        if event.event_id == event_id:
            raise SimulatedCrash("died after COMMIT, before basic_ack")

    doomed = make_consumer(event_settings, session_factory, after_commit=crash_on_transfer)
    with pytest.raises(SimulatedCrash):
        for _ in range(2):
            method, properties, body = _first_delivery(doomed)
            doomed.handle_delivery(doomed.channel, method, properties, body)
    doomed.close()

    assert _count(session_factory, EventAuditEntry, event_id) == 1  # effect committed
    assert queue_depth(event_settings, topology_of(event_settings).queue) == 1  # not acked

    survivor = make_consumer(event_settings, session_factory)
    method, properties, body = _first_delivery(survivor)
    assert method.redelivered is True
    assert survivor.handle_delivery(survivor.channel, method, properties, body) == Outcome.DUPLICATE
    survivor.close()
    assert _count(session_factory, EventAuditEntry, event_id) == 1  # still exactly once
    assert queue_depth(event_settings, topology_of(event_settings).queue) == 0  # now acked


# --- invalid events, retries, DLQ --------------------------------------------------------------


def test_malformed_events_go_straight_to_the_dlq(
    event_settings: Settings, session_factory: Factory
) -> None:
    declare(event_settings)
    valid = {
        "event_id": str(uuid.uuid4()),
        "event_type": TRANSFER_COMPLETED,
        "event_version": 1,
        "aggregate_id": str(uuid.uuid4()),
        "occurred_at": "2026-11-11T12:00:00+00:00",
        "payload": {},
    }
    bodies = [
        b"{not json",
        json.dumps({**valid, "event_version": 99}).encode(),
        json.dumps(valid).encode(),  # payload missing every required field
    ]
    for body in bodies:
        _raw_publish(event_settings, body)

    consumer = make_consumer(event_settings, session_factory)
    assert drain(consumer, event_settings) == [Outcome.DEAD_LETTERED] * 3
    consumer.close()

    dead = get_messages(event_settings, topology_of(event_settings).dead_letter_queue)
    assert [body for _, body in dead] == bodies
    reasons = [props.headers[ERROR_HEADER] for props, _ in dead]
    assert all(r.startswith("invalid event") for r in reasons)
    assert "unsupported event" in reasons[1]
    assert _count(session_factory, ProcessedEvent) == 0  # nothing half-processed


class FlakyHandler(AuditHandler):
    def __init__(self, *args, failures: int, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.failures_left = failures
        self.calls = 0

    def handle(self, event, *, delivery_attempt, redelivered=False):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.failures_left > 0:
            self.failures_left -= 1
            raise OperationalError("INSERT", {}, Exception("database is restarting"))
        return super().handle(event, delivery_attempt=delivery_attempt, redelivered=redelivered)


def _consumer_with(settings: Settings, handler: AuditHandler):  # type: ignore[no-untyped-def]
    from app.events.consumer import EventConsumer

    consumer = EventConsumer(settings, handler)
    consumer.open()
    return consumer


def test_transient_failures_are_retried_with_backoff_then_succeed(
    event_settings: Settings, session_factory: Factory
) -> None:
    create_account(session_factory, funded=100)  # one account.funded event
    make_publisher(event_settings, session_factory).publish_once()
    handler = FlakyHandler(session_factory, event_settings.consumer_name, failures=2)
    consumer = _consumer_with(event_settings, handler)

    outcomes = drain(consumer, event_settings)
    consumer.close()

    assert outcomes == [Outcome.RETRY_SCHEDULED, Outcome.RETRY_SCHEDULED, Outcome.PROCESSED]
    with session_factory() as session:
        audit = session.scalars(select(EventAuditEntry)).one()
    assert audit.delivery_attempt == 3
    assert queue_depth(event_settings, topology_of(event_settings).dead_letter_queue) == 0


def test_retry_exhaustion_routes_to_the_dlq(
    event_settings: Settings, session_factory: Factory
) -> None:
    create_account(session_factory, funded=100)
    make_publisher(event_settings, session_factory).publish_once()
    handler = FlakyHandler(session_factory, event_settings.consumer_name, failures=99)
    consumer = _consumer_with(event_settings, handler)

    outcomes = drain(consumer, event_settings)
    consumer.close()

    retries = len(event_settings.consumer_retry_delays_ms)
    assert outcomes == [Outcome.RETRY_SCHEDULED] * retries + [Outcome.DEAD_LETTERED]
    assert handler.calls == retries + 1  # bounded: no infinite requeue loop
    ((properties, _),) = get_messages(event_settings, topology_of(event_settings).dead_letter_queue)
    assert properties.headers[ERROR_HEADER].startswith(f"retries exhausted after {retries} retries")
    assert _count(session_factory, ProcessedEvent) == 0


def test_permanent_processing_errors_are_dead_lettered_without_retries(
    event_settings: Settings, session_factory: Factory
) -> None:
    create_account(session_factory, funded=100)
    make_publisher(event_settings, session_factory).publish_once()
    with session_factory() as session, session.begin():  # break the effect table on purpose
        session.execute(
            text(
                "ALTER TABLE event_audit_log ADD CONSTRAINT ck_test_reject "
                "CHECK (amount_minor < 0) NOT VALID"
            )
        )
    try:
        consumer = make_consumer(event_settings, session_factory)
        assert drain(consumer, event_settings) == [Outcome.DEAD_LETTERED]
        consumer.close()
    finally:
        with session_factory() as session, session.begin():
            session.execute(text("ALTER TABLE event_audit_log DROP CONSTRAINT ck_test_reject"))
    ((properties, _),) = get_messages(event_settings, topology_of(event_settings).dead_letter_queue)
    assert properties.headers[ERROR_HEADER].startswith("permanent: constraint violation")


def test_poison_message_that_keeps_crashing_hits_the_broker_delivery_limit(
    event_settings: Settings,
) -> None:
    """If a message kills the consumer every time (no chance to retry or dead-letter it
    in code), the quorum queue's x-delivery-limit dead-letters it."""
    declare(event_settings)
    _raw_publish(event_settings, b'{"poison": true}')
    topology = topology_of(event_settings)
    connection = connect(event_settings)
    channel = connection.channel()
    deliveries = 0
    while True:
        method, _, _ = channel.basic_get(topology.queue)
        if method is None:
            break
        deliveries += 1
        channel.basic_nack(method.delivery_tag, requeue=True)  # "crash": never acked
        assert deliveries <= 10
    connection.close()
    wait_for(lambda: queue_depth(event_settings, topology.dead_letter_queue) == 1, what="DLQ")
    assert deliveries == event_settings.consumer_delivery_limit + 1


def test_prefetch_bounds_unacknowledged_deliveries(
    event_settings: Settings, session_factory: Factory
) -> None:
    for _ in range(6):
        create_account(session_factory, funded=10)
    make_publisher(event_settings, session_factory).publish_once()
    settings = event_settings.model_copy(update={"consumer_prefetch": 2})
    consumer = make_consumer(settings, session_factory)
    received: list[int] = []
    consumer.channel.basic_consume(
        topology_of(settings).queue,
        on_message_callback=lambda ch, m, p, b: received.append(m.delivery_tag),  # never acks
    )
    connection = consumer._connection
    assert connection is not None
    connection.process_data_events(time_limit=1)
    assert len(received) == 2  # the broker stops at the prefetch window
    consumer.close()
    assert queue_depth(settings, topology_of(settings).queue) == 6  # all requeued


# --- the real run loops ---------------------------------------------------------------------


def test_run_loops_deliver_end_to_end_and_shut_down_gracefully(
    event_settings: Settings, session_factory: Factory
) -> None:
    publisher = make_publisher(event_settings, session_factory)
    consumer = make_consumer(event_settings, session_factory)
    consumer.close()
    stop = threading.Event()
    threads = [
        threading.Thread(target=publisher.run_forever, args=(stop,)),
        threading.Thread(target=consumer.run_forever, args=(stop,)),
    ]
    for thread in threads:
        thread.start()
    try:
        event_ids = [_one_transfer(session_factory) for _ in range(5)]
        wait_for(lambda: _count(session_factory, ProcessedEvent) == 10, what="10 events processed")
    finally:
        stop.set()
        for thread in threads:
            thread.join(15)
    assert not any(t.is_alive() for t in threads)
    assert all(_count(session_factory, EventAuditEntry, e) == 1 for e in event_ids)
    assert _statuses(session_factory) == {"published": 10}
    assert reconcile(session_factory).ok


def test_topology_declaration_is_idempotent(event_settings: Settings) -> None:
    connection = connect(event_settings)
    for _ in range(2):
        declare_topology(connection.channel(), topology_of(event_settings))
    connection.close()


def test_persistent_properties(event_settings: Settings) -> None:
    properties = persistent_properties(message_id="m", event_type="t", correlation_id="c")
    assert properties.delivery_mode == pika.DeliveryMode.Persistent.value
