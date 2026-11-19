"""Outbox publisher: moves committed outbox rows to RabbitMQ.

Run:  python -m app.events.publisher

Per batch:
  1. CLAIM   short transaction: up to N due rows -> 'processing' under a lease and a
             fresh fencing token (FOR UPDATE SKIP LOCKED: concurrent publishers never
             block each other or claim the same row). COMMIT. No locks are held after this.
  2. PUBLISH outside any database transaction: persistent messages, mandatory routing,
             publisher confirms. basic_publish returns only once the broker has confirmed.
  3. RECORD  short transaction: confirmed rows -> 'published'; failed rows -> 'pending'
             with exponential backoff + jitter. Both only for rows still held under OUR
             token.

AT-LEAST-ONCE, NOT EXACTLY-ONCE: if the process dies between 2 and 3 (broker confirmed,
outbox not updated) or a confirm is lost with the connection, the lease expires, another
claim publishes the event again, and consumers see a duplicate. They deduplicate by
event_id. Without the outbox the failure would be worse: the event could be lost.
"""

import contextlib
import logging
import signal
import threading
import uuid
from dataclasses import dataclass, field

import pika
from pika.adapters.blocking_connection import BlockingChannel
from pika.exceptions import AMQPError, NackError, UnroutableError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.core.database import create_db_engine, create_session_factory
from app.core.logging import configure_logging
from app.events.backoff import backoff_ms
from app.events.heartbeat import beat
from app.events.rabbitmq import Topology, connect, declare_topology, persistent_properties
from app.repositories.outbox_repository import ClaimedEvent, OutboxRepository

logger = logging.getLogger(__name__)


@dataclass
class PublishReport:
    claimed: int = 0
    published: int = 0
    failed: dict[str, list[uuid.UUID]] = field(default_factory=dict)

    @property
    def failed_count(self) -> int:
        return sum(len(ids) for ids in self.failed.values())


class OutboxPublisher:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        *,
        topology: Topology | None = None,
        connect_fn=connect,  # type: ignore[no-untyped-def]  # injectable for tests
    ) -> None:
        self._settings = settings
        self._sessions = session_factory
        self._topology = topology or Topology.from_settings(settings)
        self._connect = connect_fn
        self._connection: pika.BlockingConnection | None = None
        self._channel: BlockingChannel | None = None

    # --- broker connection ---------------------------------------------------------------

    def _ensure_channel(self) -> BlockingChannel:
        if self._channel is not None and self._channel.is_open:
            return self._channel
        self.close()
        self._connection = self._connect(self._settings)
        channel = self._connection.channel()
        channel.confirm_delivery()  # every basic_publish now waits for the broker's ack
        declare_topology(channel, self._topology)
        self._channel = channel
        logger.info("Publisher connected to RabbitMQ exchange=%s", self._topology.exchange)
        return channel

    def close(self) -> None:
        connection, self._connection, self._channel = self._connection, None, None
        if connection is not None and connection.is_open:
            with contextlib.suppress(AMQPError):
                connection.close()

    # --- one batch -------------------------------------------------------------------------

    def publish_once(self) -> PublishReport:
        """Claim, publish and record one batch. Raises AMQPError if the broker is down
        BEFORE anything was claimed; never leaves claimed rows without an outcome except
        on process death (then the lease covers it)."""
        channel = self._ensure_channel()  # connect first: don't claim what we can't send
        token = uuid.uuid4()
        with self._sessions() as session, session.begin():
            batch = OutboxRepository(session).claim_batch(
                batch_size=self._settings.outbox_batch_size,
                lease_seconds=self._settings.outbox_lease_seconds,
                token=token,
            )
        report = PublishReport(claimed=len(batch))
        if not batch:
            return report

        published: list[uuid.UUID] = []
        for index, event in enumerate(batch):
            try:
                self._publish(channel, event)
                published.append(event.id)
            except UnroutableError:
                # No queue is bound for this routing key. Retrying later is right (the
                # consumer may not have declared its queue yet), but it is a config error.
                logger.error("Outbox event %s (%s) is unroutable", event.id, event.event_type)
                report.failed.setdefault("unroutable: no queue bound", []).append(event.id)
            except NackError:
                report.failed.setdefault("nacked by broker", []).append(event.id)
            except AMQPError as exc:
                # Connection or channel lost mid-batch. Messages after this one were not
                # sent; THIS one may or may not have reached the broker (ambiguous), so it
                # is retried too and consumers will deduplicate if it did arrive.
                logger.warning("Broker connection lost while publishing: %r", exc)
                rest = [e.id for e in batch[index:]]
                report.failed.setdefault(f"broker connection lost: {exc!r}", []).extend(rest)
                self.close()
                break

        with self._sessions() as session, session.begin():
            repo = OutboxRepository(session)
            report.published = repo.mark_published(published, token=token)
            for error, ids in report.failed.items():
                repo.release_for_retry(
                    ids,
                    token=token,
                    error=error,
                    base_ms=self._settings.outbox_retry_base_ms,
                    cap_ms=self._settings.outbox_retry_max_ms,
                )
        if report.published != len(published):
            logger.warning(
                "Lost the claim on %d confirmed event(s) (lease expired); they will be "
                "published again",
                len(published) - report.published,
            )
        return report

    def _publish(self, channel: BlockingChannel, event: ClaimedEvent) -> None:
        channel.basic_publish(
            exchange=self._topology.exchange,
            routing_key=event.event_type,
            body=event.envelope().to_json(),
            properties=persistent_properties(
                message_id=str(event.id),
                event_type=event.event_type,
                correlation_id=event.correlation_id,
                headers={"event_version": event.event_version},
            ),
            mandatory=True,  # unroutable -> UnroutableError instead of silently dropped
        )

    # --- main loop -------------------------------------------------------------------------

    def run_forever(self, stop: threading.Event) -> None:
        failures = 0
        while not stop.is_set():
            beat()
            try:
                report = self.publish_once()
                failures = 0
            except (AMQPError, OSError, SQLAlchemyError) as exc:
                failures += 1
                delay = backoff_ms(
                    failures,
                    base_ms=self._settings.outbox_retry_base_ms,
                    cap_ms=self._settings.outbox_retry_max_ms,
                )
                logger.warning("Publisher cannot make progress (%r); retrying in %d ms", exc, delay)
                self.close()
                stop.wait(delay / 1000)
                continue
            if report.claimed:
                logger.info(
                    "Outbox batch claimed=%d published=%d failed=%d",
                    report.claimed,
                    report.published,
                    report.failed_count,
                )
            if report.claimed < self._settings.outbox_batch_size:
                self._idle(self._settings.outbox_poll_interval_ms / 1000, stop)
        self.close()

    def _idle(self, seconds: float, stop: threading.Event) -> None:
        # connection.sleep() keeps servicing AMQP heartbeats while we wait.
        if self._connection is not None and self._connection.is_open:
            try:
                self._connection.sleep(seconds)
                return
            except AMQPError:
                self.close()
        stop.wait(seconds)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    engine = create_db_engine(settings)
    publisher = OutboxPublisher(settings, create_session_factory(engine))
    logger.info("Outbox publisher starting (batch=%d)", settings.outbox_batch_size)
    try:
        publisher.run_forever(stop)
    finally:
        engine.dispose()
        logger.info("Outbox publisher stopped")


if __name__ == "__main__":
    main()
