"""Per-consumer dead-letter routing policy (Phase 6B).

Every main queue was declared (before 6B) with `x-dead-letter-exchange=<prefix>.events.dlx`,
a FANOUT shared by all consumers, and no dead-letter routing key. Queue arguments are
immutable, so the queues (and the messages in them) are kept and the routing is fixed
around them:

    main queue --(broker dead letter, routing key = DLQ name from THIS policy)-->
    <prefix>.events.dlx (fanout) --> <prefix>.events.dlx.by-queue (direct)
        --(routing key)--> <prefix>.<consumer>.dlq             only this consumer's DLQ
        --(no match)-----> alternate exchange -> unrouted DLQ   kept, never dropped

RabbitMQ merges policy keys with queue arguments per key: the queue's argument still names
the exchange, and the policy supplies the `dead-letter-routing-key` the queue never set.
(A headers exchange cannot do this: it ignores `x-` headers such as x-last-death-queue.)

Policies live in the management API, not AMQP, so the consumer applies its own policy
with the admin credentials it already holds, at start-up, idempotently. If the API is
unreachable the consumer still runs; broker-initiated dead letters then land in the
unrouted DLQ (visible in the live topology) until the policy is applied.
"""

import logging
import re
from urllib.parse import quote

import httpx

from app.core.config import Settings
from app.events.rabbitmq import Topology

logger = logging.getLogger(__name__)

POLICY_PRIORITY = 10


def policy_name(topology: Topology) -> str:
    return f"{topology.prefix}-dead-letter-{topology.consumer_name}"


def policy_body(topology: Topology) -> dict:
    return {
        "pattern": f"^{re.escape(topology.queue)}$",
        "apply-to": "queues",
        "priority": POLICY_PRIORITY,
        "definition": {"dead-letter-routing-key": topology.dead_letter_queue},
    }


def _client(settings: Settings) -> httpx.Client | None:
    if settings.rabbitmq_management_url is None or settings.rabbitmq_password is None:
        return None
    return httpx.Client(
        base_url=settings.rabbitmq_management_url,
        auth=(settings.rabbitmq_user, settings.rabbitmq_password.get_secret_value()),
        timeout=httpx.Timeout(5.0),
    )


def _path(settings: Settings, topology: Topology) -> str:
    vhost = quote(settings.rabbitmq_vhost, safe="")
    return f"/api/policies/{vhost}/{quote(policy_name(topology), safe='')}"


def ensure_dead_letter_policy(settings: Settings, topology: Topology) -> bool:
    """Apply this consumer's routing policy. Returns False (and logs) if it could not."""
    client = _client(settings)
    if client is None:
        logger.warning(
            "RABBITMQ_MANAGEMENT_URL not set: dead-letter policy for %s not applied; "
            "broker dead letters will be kept in %s",
            topology.queue,
            topology.unrouted_dead_letter_queue,
        )
        return False
    try:
        with client:
            response = client.put(_path(settings, topology), json=policy_body(topology))
        if response.status_code not in (201, 204):
            raise httpx.HTTPStatusError(
                f"HTTP {response.status_code}", request=response.request, response=response
            )
    except httpx.HTTPError as exc:
        logger.warning("Dead-letter policy for %s not applied: %r", topology.queue, exc)
        return False
    return True


def remove_dead_letter_policy(settings: Settings, topology: Topology) -> None:
    client = _client(settings)
    if client is None:
        return
    with client:
        client.delete(_path(settings, topology))
