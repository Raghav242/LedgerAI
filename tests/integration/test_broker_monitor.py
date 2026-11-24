"""The RabbitMQ monitoring user against the REAL broker (Phase 6A).

Provisions a throw-away monitoring user with the same code the Compose job runs, then
proves it can read what the topology probe needs (health check, queue depths) and can do
nothing else over AMQP: no declare, no publish, no consume. The user is deleted afterwards;
the development topology and messages are only read, never changed.
"""

import uuid
from collections.abc import Iterator

import httpx
import pika
import pytest
from pika.exceptions import ChannelClosedByBroker, ProbableAccessDeniedError
from pydantic import SecretStr

from app.core.config import Settings
from app.events.broker_monitor_setup import provision
from app.observability.topology import TopologyProbe

pytestmark = pytest.mark.rabbitmq

MANAGEMENT_URL = "http://127.0.0.1:15672"


@pytest.fixture
def admin(rabbit_available: Settings) -> Iterator[httpx.Client]:
    assert rabbit_available.rabbitmq_password is not None
    auth = (rabbit_available.rabbitmq_user, rabbit_available.rabbitmq_password.get_secret_value())
    with httpx.Client(base_url=MANAGEMENT_URL, auth=auth, timeout=10) as client:
        try:
            client.get("/api/overview").raise_for_status()
        except httpx.HTTPError as exc:
            pytest.skip(f"RabbitMQ management API not reachable: {exc!r}")
        yield client


@pytest.fixture
def monitor(admin: httpx.Client) -> Iterator[tuple[str, str]]:
    user, password = f"test-monitor-{uuid.uuid4().hex[:8]}", uuid.uuid4().hex
    provision(admin, user=user, password=password, vhost="/")
    try:
        yield user, password
    finally:
        admin.delete(f"/api/users/{user}")


def test_monitoring_user_can_read_health_and_queue_depths(
    monitor: tuple[str, str], rabbit_available: Settings
) -> None:
    user, password = monitor
    with httpx.Client(base_url=MANAGEMENT_URL, auth=(user, password), timeout=10) as client:
        assert client.get("/api/health/checks/alarms").status_code == 200
        queues = client.get("/api/queues/%2F").raise_for_status().json()
        assert isinstance(queues, list)
        # Cannot administer anything through the management API either.
        assert client.get("/api/users").status_code in (401, 403)
        denied = client.put("/api/users/should-not-exist", json={"password": "x", "tags": ""})
        assert denied.status_code in (401, 403)

    settings = rabbit_available.model_copy(
        update={
            "rabbitmq_management_url": MANAGEMENT_URL,
            "rabbitmq_monitor_user": user,
            "rabbitmq_monitor_password": SecretStr(password),
        }
    )
    probe = TopologyProbe(
        settings=settings,
        session_factory=None,  # type: ignore[arg-type]
        redis_client=None,
        tracing_enabled=False,
    )
    try:
        result = probe.probe_broker()
    finally:
        probe.close()
    assert result is not None and result.state == "ok"


def _amqp(settings: Settings, user: str, password: str) -> pika.BlockingConnection:
    return pika.BlockingConnection(
        pika.ConnectionParameters(
            host=settings.rabbitmq_host,
            port=settings.rabbitmq_port,
            credentials=pika.PlainCredentials(user, password),
            connection_attempts=1,
            socket_timeout=5,
        )
    )


@pytest.mark.parametrize("action", ["declare", "server_named_queue", "publish", "consume"])
def test_monitoring_user_cannot_change_or_read_messages(
    monitor: tuple[str, str], rabbit_available: Settings, action: str
) -> None:
    user, password = monitor
    try:
        connection = _amqp(rabbit_available, user, password)
    except ProbableAccessDeniedError:
        return  # even stricter: no AMQP access at all
    try:
        channel = connection.channel()
        with pytest.raises(ChannelClosedByBroker) as refused:
            if action == "declare":
                channel.queue_declare(f"test-forbidden-{uuid.uuid4().hex[:6]}")
            elif action == "server_named_queue":
                channel.queue_declare("", exclusive=True)
            elif action == "publish":
                channel.confirm_delivery()
                channel.basic_publish("ledgerflow.events", "transfer.completed", b"{}")
            else:
                channel.basic_get("ledgerflow.ledger-audit.dlq")
        assert refused.value.reply_code == 403  # ACCESS_REFUSED
    finally:
        if connection.is_open:
            connection.close()
