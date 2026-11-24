"""Per-consumer dead-letter routing against the REAL broker (Phase 6B).

Two consumers share one topology prefix (as ledger-audit and anomaly-detector share
`ledgerflow`). A message that dies in one consumer's queue must reach that consumer's DLQ
and no other. Also covers the in-place migration from the pre-6B topology (every DLQ
bound directly to the shared fanout) without losing messages already dead-lettered.
"""

import time
import uuid
from collections.abc import Iterator

import pika
import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.events.consumer import ERROR_HEADER, EventConsumer, Outcome
from app.events.dead_letter_policy import ensure_dead_letter_policy
from app.events.rabbitmq import ROUTED_EVENT_TYPES, Topology, connect, declare_topology
from tests.integration.messaging import (
    MANAGEMENT_URL,
    TEST_RETRY_DELAYS_MS,
    delete_topology,
    drain,
    get_messages,
    make_consumer,
    queue_depth,
    wait_for,
)

pytestmark = pytest.mark.rabbitmq


@pytest.fixture
def pair(rabbit_available: Settings) -> Iterator[tuple[Settings, Settings]]:
    prefix = f"test-{uuid.uuid4().hex[:10]}"
    base = {
        "rabbitmq_management_url": MANAGEMENT_URL,
        "events_topology_prefix": prefix,
        "consumer_retry_delays_ms": TEST_RETRY_DELAYS_MS,
        "consumer_delivery_limit": 2,
    }
    audit = rabbit_available.model_copy(update={**base, "consumer_name": "audit"})
    detector = rabbit_available.model_copy(update={**base, "consumer_name": "detector"})
    yield audit, detector
    delete_topology(audit)
    delete_topology(detector)


def _topology(settings: Settings) -> Topology:
    return Topology.from_settings(settings)


def _declare(*settings: Settings) -> None:
    """What every consumer does at start-up (EventConsumer.open)."""
    connection = connect(settings[0])
    for s in settings:
        declare_topology(connection.channel(), _topology(s))
        assert ensure_dead_letter_policy(s, _topology(s))
    connection.close()


def _declare_legacy(settings: Settings) -> None:
    """The pre-6B topology: DLQ bound straight to the shared fanout DLX."""
    t = _topology(settings)
    connection = connect(settings)
    ch = connection.channel()
    ch.exchange_declare(t.exchange, exchange_type="topic", durable=True)
    ch.exchange_declare(t.dead_letter_exchange, exchange_type="fanout", durable=True)
    ch.queue_declare(
        t.queue,
        durable=True,
        arguments={
            "x-queue-type": "quorum",
            "x-dead-letter-exchange": t.dead_letter_exchange,
            "x-delivery-limit": settings.consumer_delivery_limit,
        },
    )
    for event_type in ROUTED_EVENT_TYPES:
        ch.queue_bind(t.queue, t.exchange, routing_key=event_type)
    ch.queue_declare(t.dead_letter_queue, durable=True, arguments={"x-queue-type": "quorum"})
    ch.queue_bind(t.dead_letter_queue, t.dead_letter_exchange)
    connection.close()


def _poison(settings: Settings) -> None:
    """Make the broker dead-letter one message from this consumer's main queue.

    Quorum-queue requeues are asynchronous, so an empty basic_get does not mean the message
    is gone: keep "crashing" on it until the broker has dead-lettered it (delivery limit).
    """
    t = _topology(settings)
    connection = connect(settings)
    ch = connection.channel()
    ch.basic_publish("", t.queue, b'{"poison": true}', pika.BasicProperties(delivery_mode=2))
    deadline = time.monotonic() + 15
    deliveries = 0
    while time.monotonic() < deadline:
        method, _, _ = ch.basic_get(t.queue)
        if method is not None:
            deliveries += 1
            ch.basic_nack(method.delivery_tag, requeue=True)  # a crash, every time
        elif deliveries > settings.consumer_delivery_limit:
            break  # delivered limit + 1 times: the broker dead-lettered it
        else:
            time.sleep(0.05)
    connection.close()
    assert deliveries == settings.consumer_delivery_limit + 1


def _depths(*settings: Settings) -> list[int]:
    return [queue_depth(s, _topology(s).dead_letter_queue) for s in settings]


def test_broker_dead_letters_reach_only_the_consumer_they_died_in(
    pair: tuple[Settings, Settings],
) -> None:
    audit, detector = pair
    _declare(audit, detector)
    _poison(audit)
    wait_for(lambda: _depths(audit, detector) == [1, 0], what="audit DLQ only")
    _poison(detector)
    wait_for(lambda: _depths(audit, detector) == [1, 1], what="one each")
    assert queue_depth(audit, _topology(audit).unrouted_dead_letter_queue) == 0
    ((properties, _),) = get_messages(detector, _topology(detector).dead_letter_queue)
    assert properties.headers["x-last-death-queue"] == _topology(detector).queue
    assert properties.headers["x-last-death-reason"] == "delivery_limit"


def test_without_the_policy_broker_dead_letters_are_kept_unrouted(
    pair: tuple[Settings, Settings],
) -> None:
    audit, detector = pair
    connection = connect(audit)
    for s in (audit, detector):
        declare_topology(connection.channel(), _topology(s))  # no policy applied
    connection.close()
    _poison(audit)
    unrouted = _topology(audit).unrouted_dead_letter_queue
    wait_for(lambda: queue_depth(audit, unrouted) == 1, what="unrouted DLQ")
    assert _depths(audit, detector) == [0, 0]  # never copied into the wrong DLQ


def test_consumer_dead_letters_reach_only_its_own_dlq(
    pair: tuple[Settings, Settings], session_factory: sessionmaker[Session]
) -> None:
    audit, detector = pair
    _declare(audit, detector)
    connection = connect(audit)
    connection.channel().basic_publish(
        _topology(audit).exchange,
        ROUTED_EVENT_TYPES[0],
        b"not json",
        pika.BasicProperties(delivery_mode=2, message_id=str(uuid.uuid4())),
    )
    connection.close()
    # Both queues receive the routed event; each consumer rejects its own copy.
    for settings in (audit, detector):
        consumer: EventConsumer = make_consumer(settings, session_factory)
        assert drain(consumer, settings) == [Outcome.DEAD_LETTERED]
        consumer.close()
    assert _depths(audit, detector) == [1, 1]  # one copy each, not two each
    for settings in (audit, detector):
        ((properties, _),) = get_messages(settings, _topology(settings).dead_letter_queue)
        assert properties.headers[ERROR_HEADER].startswith("invalid event")
        assert properties.headers["x-ledgerflow-consumer"] == settings.consumer_name


def test_migration_from_the_shared_fanout_keeps_existing_dead_letters(
    pair: tuple[Settings, Settings],
) -> None:
    audit, detector = pair
    _declare_legacy(audit)
    _declare_legacy(detector)
    _poison(audit)  # the old bug: one failure, a copy in BOTH DLQs
    wait_for(lambda: _depths(audit, detector) == [1, 1], what="legacy fan-out copies")

    _declare(audit, detector)  # what every consumer does at start-up
    _declare(audit, detector)  # idempotent
    assert _depths(audit, detector) == [1, 1]  # nothing lost by the migration

    _poison(audit)
    wait_for(lambda: _depths(audit, detector) == [2, 1], what="routed after migration")
    _poison(detector)
    wait_for(lambda: _depths(audit, detector) == [2, 2], what="routed after migration")


def test_unattributable_dead_letters_are_kept_not_dropped(
    pair: tuple[Settings, Settings],
) -> None:
    audit, detector = pair
    _declare(audit, detector)
    connection = connect(audit)
    connection.channel().basic_publish(
        _topology(audit).dead_letter_exchange, "", b"orphan", pika.BasicProperties(delivery_mode=2)
    )
    connection.close()
    unrouted = _topology(audit).unrouted_dead_letter_queue
    wait_for(lambda: queue_depth(audit, unrouted) == 1, what="unrouted DLQ")
    assert _depths(audit, detector) == [0, 0]
