"""RabbitMQ connection and topology.

Topology (all durable):

    <prefix>.events        topic exchange   producers publish with routing key = event type
    <prefix>.events.dlx    fanout exchange  dead letters
    <prefix>.<consumer>                 quorum queue  bound to the event types it handles;
                                        x-delivery-limit + DLX catch poison messages that
                                        keep crashing the consumer
    <prefix>.<consumer>.retry.<n>       classic queues with a fixed TTL (one per retry
                                        tier); expired messages dead-letter straight back
                                        into the main queue -> delayed retry, no busy loop
    <prefix>.<consumer>.dlq             quorum queue bound to the DLX

Both the publisher and the consumer declare it (idempotently, identical arguments), so
start-up order does not matter.
"""

from dataclasses import dataclass

import pika
from pika.adapters.blocking_connection import BlockingChannel

from app.core.config import Settings
from app.events.schemas import ACCOUNT_FUNDED, TRANSFER_COMPLETED

ROUTED_EVENT_TYPES = (TRANSFER_COMPLETED, ACCOUNT_FUNDED)


@dataclass(frozen=True, slots=True)
class Topology:
    prefix: str
    consumer_name: str
    retry_delays_ms: tuple[int, ...]
    delivery_limit: int

    @classmethod
    def from_settings(cls, settings: Settings) -> "Topology":
        return cls(
            prefix=settings.events_topology_prefix,
            consumer_name=settings.consumer_name,
            retry_delays_ms=tuple(settings.consumer_retry_delays_ms),
            delivery_limit=settings.consumer_delivery_limit,
        )

    @property
    def exchange(self) -> str:
        return f"{self.prefix}.events"

    @property
    def dead_letter_exchange(self) -> str:
        return f"{self.prefix}.events.dlx"

    @property
    def queue(self) -> str:
        return f"{self.prefix}.{self.consumer_name}"

    @property
    def dead_letter_queue(self) -> str:
        return f"{self.queue}.dlq"

    def retry_queue(self, tier: int) -> str:
        return f"{self.queue}.retry.{tier}"

    @property
    def all_queues(self) -> list[str]:
        return [
            self.queue,
            self.dead_letter_queue,
            *(self.retry_queue(i) for i in range(1, len(self.retry_delays_ms) + 1)),
        ]


def connection_parameters(settings: Settings) -> pika.ConnectionParameters:
    if settings.rabbitmq_password is None:
        raise RuntimeError("RABBITMQ_PASSWORD is not set")
    return pika.ConnectionParameters(
        host=settings.rabbitmq_host,
        port=settings.rabbitmq_port,
        virtual_host=settings.rabbitmq_vhost,
        credentials=pika.PlainCredentials(
            settings.rabbitmq_user, settings.rabbitmq_password.get_secret_value()
        ),
        heartbeat=settings.rabbitmq_heartbeat_seconds,
        blocked_connection_timeout=30,
        socket_timeout=settings.rabbitmq_connect_timeout_seconds,
        stack_timeout=settings.rabbitmq_connect_timeout_seconds * 2,
        connection_attempts=1,  # callers own the retry/backoff policy
    )


def connect(settings: Settings) -> pika.BlockingConnection:
    return pika.BlockingConnection(connection_parameters(settings))


def declare_topology(channel: BlockingChannel, topology: Topology) -> None:
    channel.exchange_declare(topology.exchange, exchange_type="topic", durable=True)
    channel.exchange_declare(topology.dead_letter_exchange, exchange_type="fanout", durable=True)

    channel.queue_declare(
        topology.queue,
        durable=True,
        arguments={
            "x-queue-type": "quorum",
            "x-dead-letter-exchange": topology.dead_letter_exchange,
            "x-delivery-limit": topology.delivery_limit,
        },
    )
    for event_type in ROUTED_EVENT_TYPES:
        channel.queue_bind(topology.queue, topology.exchange, routing_key=event_type)

    channel.queue_declare(
        topology.dead_letter_queue, durable=True, arguments={"x-queue-type": "quorum"}
    )
    channel.queue_bind(topology.dead_letter_queue, topology.dead_letter_exchange)

    for tier, delay_ms in enumerate(topology.retry_delays_ms, start=1):
        channel.queue_declare(
            topology.retry_queue(tier),
            durable=True,
            arguments={
                "x-message-ttl": delay_ms,
                "x-dead-letter-exchange": "",  # default exchange: route by queue name
                "x-dead-letter-routing-key": topology.queue,
            },
        )


def persistent_properties(
    *, message_id: str, event_type: str, correlation_id: str | None, headers: dict | None = None
) -> pika.BasicProperties:
    return pika.BasicProperties(
        delivery_mode=pika.DeliveryMode.Persistent,
        content_type="application/json",
        message_id=message_id,
        type=event_type,
        correlation_id=correlation_id,
        headers=headers or {},
    )
