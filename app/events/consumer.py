"""Reliable event consumer (the "ledger-audit" consumer).

Run:  python -m app.events.consumer

Per delivery (manual acks, bounded prefetch):
  1. Validate envelope + payload schema/version. Invalid -> dead-letter (permanent).
  2. ONE PostgreSQL transaction:
        INSERT processed_events (event_id, consumer) ON CONFLICT DO NOTHING
        -> conflict: duplicate delivery, nothing else to do
        -> new:      INSERT the durable effect (event_audit_log row)
     COMMIT
  3. ACK only after the commit.

Crash before COMMIT: the transaction rolls back and the unacked message is redelivered.
Crash after COMMIT, before ACK: redelivered; step 2 finds the receipt and does nothing.
Either way the effect is applied exactly once, although delivery is at-least-once.

Failures: transient errors (database unavailable, lock conflicts, unknown errors) are
retried through TTL retry-tier queues (bounded, increasing delays, no hot requeue loop);
after the last tier, or for permanent errors, the message is dead-lettered with the reason.
"""

import contextlib
import logging
import os
import signal
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum

import pika
from opentelemetry.trace import SpanKind
from pika.adapters.blocking_connection import BlockingChannel
from pika.exceptions import AMQPError
from pika.spec import Basic, BasicProperties
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.core.database import atomic, create_db_engine, create_session_factory
from app.core.exceptions import ConcurrencyConflictError
from app.core.logging import configure_logging
from app.events.backoff import backoff_ms
from app.events.dead_letter_policy import ensure_dead_letter_policy
from app.events.heartbeat import beat
from app.events.rabbitmq import Topology, connect, declare_topology, persistent_properties
from app.events.schemas import EventEnvelope, InvalidEventError, parse_envelope
from app.models import EventAuditEntry, ProcessedEvent
from app.observability.heartbeats import HeartbeatReporter
from app.observability.tracing import (
    configure_tracing,
    extract_context,
    instrument_worker_engine,
    tracer,
)

logger = logging.getLogger(__name__)

RETRY_HEADER = "x-ledgerflow-retry"
ERROR_HEADER = "x-ledgerflow-error"


class Outcome(StrEnum):
    PROCESSED = "processed"
    DUPLICATE = "duplicate"
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD_LETTERED = "dead_lettered"


class PermanentEventError(Exception):
    """Processing can never succeed for this event (no point retrying)."""


# Called with (event, redelivered); may raise or hard-exit (fault injection, tests).
CrashHook = Callable[[EventEnvelope, bool], None]


def _no_hook(_event: EventEnvelope, _redelivered: bool) -> None:
    return None


def _exit_on(event_id: str | None, code: int, label: str) -> CrashHook:
    """DEV-ONLY fault injection: hard-exit the process (no cleanup, no ack) on one event.

    Fires only on the FIRST delivery. Otherwise a crash before commit would repeat on every
    redelivery (nothing was committed) until the broker's delivery limit dead-letters it --
    realistic for a poison message, but not what the crash demo wants to show.
    """
    if not event_id:
        return _no_hook

    def hook(event: EventEnvelope, redelivered: bool) -> None:
        if str(event.event_id) == event_id and not redelivered:
            logger.error("FAULT INJECTION: crashing %s for event %s", label, event_id)
            os._exit(code)

    return hook


class AuditHandler:
    """The durable effect, written together with the deduplication receipt."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        consumer_name: str,
        *,
        before_commit: CrashHook = _no_hook,
        after_commit: CrashHook = _no_hook,
    ) -> None:
        self._sessions = session_factory
        self._consumer = consumer_name
        self._before_commit = before_commit
        self._after_commit = after_commit

    def handle(
        self, event: EventEnvelope, *, delivery_attempt: int, redelivered: bool = False
    ) -> Outcome:
        try:
            with self._sessions() as session, atomic(session):
                receipt = session.execute(
                    insert(ProcessedEvent)
                    .values(event_id=event.event_id, consumer_name=self._consumer)
                    .on_conflict_do_nothing(index_elements=["event_id", "consumer_name"])
                    .returning(ProcessedEvent.event_id)
                ).first()
                if receipt is None:
                    return Outcome.DUPLICATE
                session.add(
                    EventAuditEntry(
                        event_id=event.event_id,
                        consumer_name=self._consumer,
                        event_type=event.event_type,
                        event_version=event.event_version,
                        aggregate_id=event.aggregate_id,
                        amount_minor=event.payload.get("amount_minor"),
                        correlation_id=event.correlation_id,
                        occurred_at=event.occurred_at,
                        delivery_attempt=delivery_attempt,
                    )
                )
                session.flush()
                self._before_commit(event, redelivered)
        except IntegrityError as exc:
            raise PermanentEventError(f"constraint violation: {exc.orig}") from exc
        self._after_commit(event, redelivered)
        return Outcome.PROCESSED


def is_transient(exc: Exception) -> bool:
    """Retry-worthy: the same event may well succeed a little later."""
    if isinstance(exc, PermanentEventError | InvalidEventError):
        return False
    if isinstance(exc, ConcurrencyConflictError | DBAPIError | OSError):
        return True
    return True  # unknown errors: retry (bounded), then dead-letter


class EventConsumer:
    def __init__(
        self,
        settings: Settings,
        handler: AuditHandler,
        *,
        topology: Topology | None = None,
        connect_fn=connect,  # type: ignore[no-untyped-def]
        heartbeat: HeartbeatReporter | None = None,
    ) -> None:
        self._settings = settings
        self._handler = handler
        self._topology = topology or Topology.from_settings(settings)
        self._connect = connect_fn
        self._connection: pika.BlockingConnection | None = None
        self._channel: BlockingChannel | None = None
        self._publish_channel: BlockingChannel | None = None  # confirms, for retry/DLQ
        self._stats_channel: BlockingChannel | None = None  # passive declares (queue depths)
        self.counts: dict[Outcome, int] = dict.fromkeys(Outcome, 0)
        self._heartbeat = heartbeat
        self._broker_ok_at: datetime | None = None

    # --- connection ----------------------------------------------------------------------

    @property
    def channel(self) -> BlockingChannel:
        assert self._channel is not None, "call open() first"
        return self._channel

    def open(self) -> BlockingChannel:
        self.close()
        self._connection = self._connect(self._settings)
        channel = self._connection.channel()
        declare_topology(channel, self._topology)
        ensure_dead_letter_policy(self._settings, self._topology)
        channel.basic_qos(prefetch_count=self._settings.consumer_prefetch)
        publish_channel = self._connection.channel()
        publish_channel.confirm_delivery()
        self._channel, self._publish_channel = channel, publish_channel
        return channel

    def close(self) -> None:
        connection = self._connection
        self._connection = self._channel = self._publish_channel = self._stats_channel = None
        if connection is not None and connection.is_open:
            with contextlib.suppress(AMQPError):
                connection.close()

    # --- one delivery ----------------------------------------------------------------------

    def handle_delivery(
        self,
        channel: BlockingChannel,
        method: Basic.Deliver | Basic.GetOk,
        properties: BasicProperties,
        body: bytes,
    ) -> Outcome:
        headers = properties.headers or {}
        retries_so_far = int(headers.get(RETRY_HEADER, 0))
        try:
            event, _payload = parse_envelope(body)
        except InvalidEventError as exc:
            return self._dead_letter(channel, method, properties, body, f"invalid event: {exc}")

        attempt = retries_so_far + 1
        redelivered = bool(getattr(method, "redelivered", False))
        try:
            # Continues the request's trace from the `traceparent` header (if any).
            with tracer.start_as_current_span(
                f"event.consume {self._topology.consumer_name}",
                context=extract_context(headers),
                kind=SpanKind.CONSUMER,
            ) as span:
                span.set_attribute("messaging.system", "rabbitmq")
                span.set_attribute("messaging.message.id", str(event.event_id))
                span.set_attribute("ledgerflow.event.type", event.event_type)
                span.set_attribute("ledgerflow.consumer", self._topology.consumer_name)
                span.set_attribute("ledgerflow.delivery_attempt", attempt)
                span.set_attribute("ledgerflow.redelivered", redelivered)
                outcome = self._handler.handle(
                    event, delivery_attempt=attempt, redelivered=redelivered
                )
                span.set_attribute("ledgerflow.consume.outcome", str(outcome))
        except Exception as exc:
            if not is_transient(exc):
                return self._dead_letter(channel, method, properties, body, f"permanent: {exc}")
            if retries_so_far >= len(self._topology.retry_delays_ms):
                return self._dead_letter(
                    channel,
                    method,
                    properties,
                    body,
                    f"retries exhausted after {retries_so_far} retries: {exc!r}",
                )
            return self._schedule_retry(channel, method, properties, body, retries_so_far, exc)

        channel.basic_ack(method.delivery_tag)  # only after the transaction committed
        self.counts[outcome] += 1
        if outcome is Outcome.DUPLICATE:
            logger.info(
                "Duplicate delivery of event %s ignored (redelivered=%s)",
                event.event_id,
                getattr(method, "redelivered", None),
            )
        return outcome

    def _schedule_retry(
        self,
        channel: BlockingChannel,
        method: Basic.Deliver | Basic.GetOk,
        properties: BasicProperties,
        body: bytes,
        retries_so_far: int,
        exc: Exception,
    ) -> Outcome:
        tier = retries_so_far + 1
        assert self._publish_channel is not None
        # Publish (confirmed) the copy BEFORE acking the original: a crash in between
        # yields a duplicate, never a lost event.
        self._publish_channel.basic_publish(
            exchange="",
            routing_key=self._topology.retry_queue(tier),
            body=body,
            properties=self._copy_properties(
                properties, {RETRY_HEADER: tier, ERROR_HEADER: repr(exc)[:500]}
            ),
        )
        channel.basic_ack(method.delivery_tag)
        self.counts[Outcome.RETRY_SCHEDULED] += 1
        logger.warning(
            "Transient failure for message %s; retry %d/%d in %d ms: %r",
            properties.message_id,
            tier,
            len(self._topology.retry_delays_ms),
            self._topology.retry_delays_ms[tier - 1],
            exc,
        )
        return Outcome.RETRY_SCHEDULED

    def _dead_letter(
        self,
        channel: BlockingChannel,
        method: Basic.Deliver | Basic.GetOk,
        properties: BasicProperties,
        body: bytes,
        reason: str,
    ) -> Outcome:
        assert self._publish_channel is not None
        # Straight to THIS consumer's DLQ (default exchange, routed by queue name): never
        # through the shared fanout, so no other consumer's DLQ receives a copy.
        self._publish_channel.basic_publish(
            exchange="",
            routing_key=self._topology.dead_letter_queue,
            body=body,
            properties=self._copy_properties(
                properties,
                {
                    ERROR_HEADER: reason[:1000],
                    "x-ledgerflow-consumer": self._topology.consumer_name,
                },
            ),
        )
        channel.basic_ack(method.delivery_tag)
        self.counts[Outcome.DEAD_LETTERED] += 1
        logger.error("Dead-lettered message %s: %s", properties.message_id, reason)
        return Outcome.DEAD_LETTERED

    @staticmethod
    def _copy_properties(properties: BasicProperties, extra_headers: dict) -> BasicProperties:
        return persistent_properties(
            message_id=properties.message_id or str(uuid.uuid4()),
            event_type=properties.type or "unknown",
            correlation_id=properties.correlation_id,
            headers={**(properties.headers or {}), **extra_headers},
        )

    # --- heartbeat -------------------------------------------------------------------------

    def queue_depths(self) -> list[dict]:
        """Ready / consumer counts of this consumer's queues (passive declares, read-only)."""
        if self._connection is None or not self._connection.is_open:
            return []
        roles = [(self._topology.queue, "main"), (self._topology.dead_letter_queue, "dead_letter")]
        roles += [
            (self._topology.retry_queue(i), "retry")
            for i in range(1, len(self._topology.retry_delays_ms) + 1)
        ]
        depths = []
        try:
            if self._stats_channel is None or not self._stats_channel.is_open:
                self._stats_channel = self._connection.channel()
            for queue, role in roles:
                frame = self._stats_channel.queue_declare(queue, passive=True)
                depths.append(
                    {
                        "queue": queue,
                        "role": role,
                        "ready": frame.method.message_count,
                        "consumers": frame.method.consumer_count,
                    }
                )
        except AMQPError:
            self._stats_channel = None
            return []
        return depths

    def report(self, *, problem: str | None = None, retry_in_s: float = 0) -> None:
        """Self-report for the live topology (throttled; forced when there is a problem)."""
        if self._heartbeat is None or (problem is None and not self._heartbeat.due()):
            return
        connected = problem is None and self._connection is not None and self._connection.is_open
        if connected:
            self._broker_ok_at = datetime.now(UTC)
        counts = {str(k): v for k, v in self.counts.items()}
        details = {
            "summary": (
                f"Consuming {self._topology.queue} with manual acks."
                if connected
                else "Cannot reach RabbitMQ; reconnecting with back-off."
            ),
            "consumer": self._topology.consumer_name,
            "broker_connected": connected,
            "broker_ok_at": self._broker_ok_at.isoformat() if self._broker_ok_at else None,
            "counts": counts,
            "queues": self.queue_depths() if connected else [],
            "metrics": [
                {
                    "key": "processed",
                    "label": "Processed since start",
                    "value": counts["processed"],
                },
                {"key": "duplicate", "label": "Duplicates ignored", "value": counts["duplicate"]},
                {
                    "key": "retry_scheduled",
                    "label": "Retries scheduled",
                    "value": counts["retry_scheduled"],
                },
                {
                    "key": "dead_lettered",
                    "label": "Dead-lettered",
                    "value": counts["dead_lettered"],
                },
            ],
        }
        if problem is not None:
            self._heartbeat.record_error(problem)
        self._heartbeat.beat(
            "ok" if connected else "degraded",
            details,
            force=problem is not None,
            next_within_seconds=retry_in_s,
        )

    # --- main loop -------------------------------------------------------------------------

    def run_forever(self, stop: threading.Event) -> None:
        failures = 0
        while not stop.is_set():
            try:
                channel = self.open()
                channel.basic_consume(
                    self._topology.queue,
                    on_message_callback=lambda ch, m, p, b: self.handle_delivery(ch, m, p, b),
                    auto_ack=False,
                )
                logger.info(
                    "Consuming %s (prefetch=%d)",
                    self._topology.queue,
                    self._settings.consumer_prefetch,
                )
                failures = 0
                while not stop.is_set():
                    beat()
                    self.report()
                    assert self._connection is not None
                    self._connection.process_data_events(time_limit=1)
                # Graceful shutdown: stop new deliveries; unacked ones are requeued when
                # the channel closes and will be redelivered (and deduplicated) later.
                channel.cancel()
            except (AMQPError, OSError) as exc:
                failures += 1
                delay = backoff_ms(failures, base_ms=500, cap_ms=30_000)
                logger.warning(
                    "Consumer connection problem (%r); reconnecting in %d ms", exc, delay
                )
                self.close()
                self.report(
                    problem=f"broker unavailable: {type(exc).__name__}", retry_in_s=delay / 1000
                )
                stop.wait(delay / 1000)
            finally:
                self.close()


def build_consumer(settings: Settings) -> tuple[EventConsumer, object]:
    tracing = configure_tracing(settings, service_name=f"ledgerflow-{settings.consumer_name}")
    engine = create_db_engine(settings)
    if tracing:
        instrument_worker_engine(engine)
    sessions = create_session_factory(engine)
    handler = AuditHandler(
        sessions,
        settings.consumer_name,
        before_commit=_exit_on(settings.consumer_crash_before_commit_event, 70, "before commit"),
        after_commit=_exit_on(settings.consumer_crash_after_commit_event, 71, "after commit"),
    )
    heartbeat = HeartbeatReporter(
        sessions,
        component="event-consumer",
        instance=settings.replica_id,
        interval_seconds=settings.heartbeat_interval_seconds,
    )
    return EventConsumer(settings, handler, heartbeat=heartbeat), engine


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    consumer, engine = build_consumer(settings)
    logger.info("Event consumer %s starting", settings.consumer_name)
    try:
        consumer.run_forever(stop)
    finally:
        engine.dispose()  # type: ignore[attr-defined]
        logger.info("Event consumer stopped: %s", dict(consumer.counts))


if __name__ == "__main__":
    main()
