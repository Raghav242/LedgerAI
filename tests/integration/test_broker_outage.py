"""Publisher and consumer survive a real RabbitMQ outage (a throwaway broker container).

The stack's broker is never touched. While the broker is down, transfers keep committing
(the API does not depend on RabbitMQ); their events wait in the outbox and are delivered,
exactly once in effect, after the broker returns.
"""

import shutil
import subprocess
import threading
import time
import uuid

import pytest
from pika.exceptions import AMQPError
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.events.rabbitmq import connect
from app.models import EventAuditEntry, ProcessedEvent
from tests.integration.messaging import make_consumer, make_publisher, wait_for
from tests.integration.support import create_account, reconcile, transfer

pytestmark = pytest.mark.rabbitmq

PORT = 5692
IMAGE = "rabbitmq:4.1-management-alpine"


def _broker_up(settings: Settings) -> bool:
    try:
        connect(settings).close()
        return True
    except (AMQPError, OSError):
        return False


@pytest.fixture
def disposable_broker(event_settings: Settings):  # type: ignore[no-untyped-def]
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not available")
    name = f"ledgerflow-rabbit-outage-test-{uuid.uuid4().hex[:6]}"
    password = uuid.uuid4().hex
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{PORT}:5672",
            "-e",
            "RABBITMQ_DEFAULT_USER=outage",
            "-e",
            f"RABBITMQ_DEFAULT_PASS={password}",
            IMAGE,
        ],
        check=True,
        capture_output=True,
    )
    from pydantic import SecretStr

    settings = event_settings.model_copy(
        update={
            "rabbitmq_port": PORT,
            "rabbitmq_user": "outage",
            "rabbitmq_password": SecretStr(password),
            "outbox_retry_base_ms": 100,
            "outbox_retry_max_ms": 500,
        }
    )
    try:
        wait_for(lambda: _broker_up(settings), timeout=60, what="disposable broker start")
        yield name, settings
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def _processed(factory: sessionmaker[Session]) -> int:
    with factory() as session:
        return session.scalar(select(func.count()).select_from(ProcessedEvent)) or 0


def test_publisher_and_consumer_recover_after_broker_outage(
    disposable_broker,
    session_factory: sessionmaker[Session],  # type: ignore[no-untyped-def]
) -> None:
    name, settings = disposable_broker
    publisher = make_publisher(settings, session_factory)
    consumer = make_consumer(settings, session_factory)
    consumer.close()
    stop = threading.Event()
    threads = [
        threading.Thread(target=publisher.run_forever, args=(stop,), daemon=True),
        threading.Thread(target=consumer.run_forever, args=(stop,), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        a = create_account(session_factory, funded=100_000)
        b = create_account(session_factory)
        wait_for(lambda: _processed(session_factory) == 1, what="funding event before outage")

        subprocess.run(["docker", "stop", "-t", "5", name], check=True, capture_output=True)
        outage_started = time.monotonic()
        # The API keeps working: money moves, events queue up in the outbox.
        for _ in range(5):
            assert transfer(session_factory, a, b, 100).succeeded
        with session_factory() as session:
            waiting = session.scalar(
                text("SELECT count(*) FROM outbox_events WHERE status <> 'published'")
            )
        assert waiting == 5
        wait_for(
            lambda: _attempted_while_down(session_factory),
            timeout=30,
            what="publisher retrying with backoff while the broker is down",
        )

        subprocess.run(["docker", "start", name], check=True, capture_output=True)
        wait_for(
            lambda: _processed(session_factory) == 6, timeout=90, what="all 6 events processed"
        )
        recovery_s = time.monotonic() - outage_started
    finally:
        stop.set()
        for thread in threads:
            thread.join(20)

    with session_factory() as session:
        audit_rows = session.scalar(select(func.count()).select_from(EventAuditEntry))
        unpublished = session.scalar(
            text("SELECT count(*) FROM outbox_events WHERE status <> 'published'")
        )
    assert audit_rows == 6  # one effect per event, no duplicates from the retries
    assert unpublished == 0
    assert reconcile(session_factory).ok
    assert recovery_s < 120


def _attempted_while_down(factory: sessionmaker[Session]) -> bool:
    """The publisher does not claim (and so does not burn attempts) while it cannot
    connect; it backs off at the connection level. Either way, nothing is published."""
    with factory() as session:
        published = session.scalar(
            text("SELECT count(*) FROM outbox_events WHERE status = 'published'")
        )
    return published == 1  # still only the pre-outage funding event
