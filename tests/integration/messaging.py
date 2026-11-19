"""RabbitMQ fixtures and helpers for delivery-guarantee tests (real broker + real DB).

Isolation: every test gets its own exchange/queue name prefix (`test-<random>`) on the
stack's broker and deletes everything under it afterwards. The PostgreSQL side uses the
integration test database. Nothing touches the development topology or data.
"""

import time
import uuid
from collections.abc import Callable, Iterator

import pika
import pytest
from pika.exceptions import AMQPError

from app.core.config import Settings
from app.events.consumer import AuditHandler, EventConsumer
from app.events.publisher import OutboxPublisher
from app.events.rabbitmq import Topology, connect, declare_topology

TEST_RETRY_DELAYS_MS = [50, 100, 150]


class SimulatedCrash(BaseException):
    """Stands in for the process dying: not an Exception, so no handler catches it."""


@pytest.fixture(scope="session")
def rabbit_available(test_settings: Settings) -> Settings:
    try:
        connect(test_settings).close()
    except (AMQPError, OSError, RuntimeError) as exc:
        pytest.skip(
            f"RabbitMQ not reachable ({exc!r}); start it: docker compose up -d --wait rabbitmq"
        )
    return test_settings


@pytest.fixture
def event_settings(rabbit_available: Settings) -> Iterator[Settings]:
    prefix = f"test-{uuid.uuid4().hex[:10]}"
    settings = rabbit_available.model_copy(
        update={
            "events_topology_prefix": prefix,
            "consumer_retry_delays_ms": TEST_RETRY_DELAYS_MS,
            "consumer_delivery_limit": 3,
            "consumer_prefetch": 10,
            "outbox_retry_base_ms": 1,
            "outbox_retry_max_ms": 1,
            "outbox_poll_interval_ms": 50,
        }
    )
    yield settings
    delete_topology(settings)


def topology_of(settings: Settings) -> Topology:
    return Topology.from_settings(settings)


def delete_topology(settings: Settings) -> None:
    topology = topology_of(settings)
    try:
        connection = connect(settings)
    except (AMQPError, OSError):
        return
    channel = connection.channel()
    for queue in topology.all_queues:
        channel.queue_delete(queue)
    for exchange in (topology.exchange, topology.dead_letter_exchange):
        channel.exchange_delete(exchange)
    connection.close()


def declare(settings: Settings) -> None:
    connection = connect(settings)
    declare_topology(connection.channel(), topology_of(settings))
    connection.close()


def queue_depth(settings: Settings, queue: str) -> int:
    """Ready (unacknowledged messages are NOT counted until requeued)."""
    connection = connect(settings)
    try:
        return connection.channel().queue_declare(queue, passive=True).method.message_count
    finally:
        connection.close()


def get_messages(settings: Settings, queue: str) -> list[tuple[pika.spec.BasicProperties, bytes]]:
    """Read (and ack) every message currently in `queue`."""
    connection = connect(settings)
    channel = connection.channel()
    messages = []
    while True:
        method, properties, body = channel.basic_get(queue)
        if method is None:
            break
        channel.basic_ack(method.delivery_tag)
        messages.append((properties, body))
    connection.close()
    return messages


def wait_for(
    condition: Callable[[], bool], *, timeout: float = 15, what: str = "condition"
) -> None:
    """Bounded polling of an observable condition (no fixed sleeps for correctness)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def make_consumer(settings: Settings, session_factory, **handler_hooks) -> EventConsumer:  # type: ignore[no-untyped-def]
    handler = AuditHandler(session_factory, settings.consumer_name, **handler_hooks)
    consumer = EventConsumer(settings, handler)
    consumer.open()
    return consumer


def drain(consumer: EventConsumer, settings: Settings, *, timeout: float = 15) -> list[str]:
    """Process deliveries (basic_get + the real handle_delivery) until the main queue and
    all retry tiers are empty. Returns the outcomes in order."""
    topology = topology_of(settings)
    outcomes: list[str] = []
    empty_observations = 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        method, properties, body = consumer.channel.basic_get(topology.queue)
        if method is not None:
            outcomes.append(consumer.handle_delivery(consumer.channel, method, properties, body))
            empty_observations = 0
            continue
        # A message moves retry tier -> main queue when its TTL expires. Check the tiers
        # FIRST, then the main queue, so a message in transit is seen in one of them; and
        # require "all empty" twice in a row before concluding.
        retry_depth = sum(
            queue_depth(settings, topology.retry_queue(i))
            for i in range(1, len(topology.retry_delays_ms) + 1)
        )
        if retry_depth == 0 and queue_depth(settings, topology.queue) == 0:
            empty_observations += 1
            if empty_observations >= 2:
                return outcomes
        else:
            empty_observations = 0
        time.sleep(0.02)  # polling interval while retry tiers' TTLs run
    raise AssertionError(f"queue not drained in {timeout}s; outcomes so far: {outcomes}")


def make_publisher(settings: Settings, session_factory) -> OutboxPublisher:  # type: ignore[no-untyped-def]
    return OutboxPublisher(settings, session_factory)
