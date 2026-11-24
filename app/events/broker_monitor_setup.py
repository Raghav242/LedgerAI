"""Provision the least-privilege RabbitMQ monitoring user (Phase 6A). One-shot, idempotent.

    python -m app.events.broker_monitor_setup

Runs as the `rabbitmq-monitor-setup` Compose job with the broker ADMIN credentials, which
only this job and the event workers hold. It creates (or updates) RABBITMQ_MONITOR_USER
with exactly the `monitoring` tag and these permissions:

  events vhost (RABBITMQ_VHOST)   configure "^$"  write "^$"  read "^$"   (match nothing)
  every other vhost               none
  topic permissions               none

RabbitMQ 4.x shows a monitoring user only the queues of vhosts it can access, so the user
needs access to the events vhost to read queue depths -- but "^$" matches no resource
name (not even server-named queues), so it cannot declare, bind, publish to, consume
from, purge or delete anything. The API replicas get only these monitoring credentials,
never the admin ones.

With RABBITMQ_MONITOR_PASSWORD unset it does nothing and exits 0: the independent probe is
optional and the topology falls back to what the workers report.
"""

import logging
import sys
import time
from urllib.parse import quote

import httpx

from app.core.config import Settings
from app.core.logging import configure_logging

logger = logging.getLogger("ledgerflow.broker_monitor_setup")

MONITOR_TAGS = ["monitoring"]
DENY_ALL = {"configure": "^$", "write": "^$", "read": "^$"}
WAIT_SECONDS = 90


class SetupError(RuntimeError):
    pass


def _q(value: str) -> str:
    return quote(value, safe="")


def provision(client: httpx.Client, *, user: str, password: str, vhost: str = "/") -> None:
    """Create/update `user`: monitoring tag, deny-all access to `vhost` only; verify."""
    response = client.put(
        f"/api/users/{_q(user)}", json={"password": password, "tags": "monitoring"}
    )
    if response.status_code not in (201, 204):
        raise SetupError(f"creating the monitoring user failed: HTTP {response.status_code}")

    vhosts = [v["name"] for v in client.get("/api/vhosts").raise_for_status().json()]
    if vhost not in vhosts:
        raise SetupError(f"virtual host {vhost!r} does not exist")
    for name in vhosts:
        kinds = ("topic-permissions",) if name == vhost else ("permissions", "topic-permissions")
        for kind in kinds:
            removed = client.delete(f"/api/{kind}/{_q(name)}/{_q(user)}")
            if removed.status_code not in (204, 404):
                raise SetupError(f"removing {kind} on {name!r} failed: HTTP {removed.status_code}")
    granted = client.put(f"/api/permissions/{_q(vhost)}/{_q(user)}", json=DENY_ALL)
    if granted.status_code not in (201, 204):
        raise SetupError(f"setting deny-all permissions failed: HTTP {granted.status_code}")

    # Verify the end state rather than trusting the calls above.
    info = client.get(f"/api/users/{_q(user)}").raise_for_status().json()
    tags = info.get("tags")
    tags = tags if isinstance(tags, list) else [t for t in str(tags).split(",") if t]
    if sorted(tags) != MONITOR_TAGS:
        raise SetupError(f"monitoring user has tags {tags}, expected {MONITOR_TAGS}")
    permissions = client.get(f"/api/users/{_q(user)}/permissions").raise_for_status().json()
    actual = [{"vhost": p.get("vhost"), **{k: p.get(k) for k in DENY_ALL}} for p in permissions]
    if actual != [{"vhost": vhost, **DENY_ALL}]:
        raise SetupError(f"monitoring user has unexpected permissions: {actual}")


def main() -> int:
    settings = Settings()  # type: ignore[call-arg]
    configure_logging(settings.log_level)
    if settings.rabbitmq_monitor_password is None or settings.rabbitmq_management_url is None:
        logger.info("RABBITMQ_MONITOR_PASSWORD or RABBITMQ_MANAGEMENT_URL unset: nothing to do")
        return 0
    if settings.rabbitmq_password is None:
        logger.error("RABBITMQ_PASSWORD (admin) is required to provision the monitoring user")
        return 1
    if settings.rabbitmq_monitor_user == settings.rabbitmq_user:
        logger.error("RABBITMQ_MONITOR_USER must differ from the admin user RABBITMQ_USER")
        return 1
    monitor_password = settings.rabbitmq_monitor_password.get_secret_value()
    if monitor_password == settings.rabbitmq_password.get_secret_value():
        logger.error("RABBITMQ_MONITOR_PASSWORD must differ from RABBITMQ_PASSWORD")
        return 1

    auth = (settings.rabbitmq_user, settings.rabbitmq_password.get_secret_value())
    deadline = time.monotonic() + WAIT_SECONDS
    with httpx.Client(
        base_url=settings.rabbitmq_management_url, auth=auth, timeout=httpx.Timeout(5.0)
    ) as client:
        while True:
            try:
                provision(
                    client,
                    user=settings.rabbitmq_monitor_user,
                    password=monitor_password,
                    vhost=settings.rabbitmq_vhost,
                )
                break
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if time.monotonic() > deadline:
                    logger.error("Management API not usable after %ss: %r", WAIT_SECONDS, exc)
                    return 1
                time.sleep(2)
            except SetupError as exc:
                logger.error("%s", exc)
                return 1
    logger.info(
        "RabbitMQ monitoring user %r provisioned: tag 'monitoring', deny-all access to %r only",
        settings.rabbitmq_monitor_user,
        settings.rabbitmq_vhost,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
