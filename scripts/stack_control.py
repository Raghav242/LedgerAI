"""Control the LOCAL Docker Compose stack (used by demos, stack tests and benchmarks).

Every action that stops, starts or reconfigures containers first checks that the target
API is on this machine; failure simulations are local-only by construction.
"""

import os
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
import redis

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_URL = "http://127.0.0.1:8080"
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
RATE_LIMIT_PREFIX = "ledgerflow:rl"
HOST_REDIS_URL = "redis://127.0.0.1:6379/0"  # the stack's Redis, published on loopback


def ensure_local(base_url: str) -> None:
    host = urlparse(base_url).hostname
    if host not in LOCAL_HOSTS:
        raise SystemExit(f"Refusing to control containers for non-local target {base_url!r}")


def compose(
    *args: str, env: dict[str, str] | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=ROOT,
        env={**os.environ, **(env or {})},
        check=check,
        capture_output=True,
        text=True,
    )


def deploy(overrides: dict[str, str] | None = None, *, profiles: tuple[str, ...] = ()) -> None:
    """(Re)create the stack with environment overrides; only changed services restart."""
    profile_args = [arg for p in profiles for arg in ("--profile", p)]
    # --build: `up` alone reuses an existing image even if the source changed since it was
    # built, so tests and demos could silently exercise stale code. Cached layers make
    # this a few seconds when nothing changed.
    result = compose(*profile_args, "up", "-d", "--build", "--wait", env=overrides, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"docker compose up failed:\n{result.stdout}\n{result.stderr}")
    # `--wait` means the containers are healthy, not that NGINX routes to them yet: after
    # an all-at-once recreate it may still hold every replica in fail_timeout. Return only
    # once traffic reaches every running replica.
    wait_until_served_only_by(DEFAULT_BASE_URL, running_replicas(), timeout=60)


def running_replicas() -> set[str]:
    running = compose("ps", "--services", "--status", "running").stdout.split()
    return {service for service in running if service.startswith("api-")}


def remove_extra_replicas() -> None:
    compose("--profile", "scale4", "rm", "-s", "-f", "api-3", "api-4", check=False)


def restore_default() -> None:
    """Back to the default configuration: two replicas, default quotas, dev database."""
    remove_extra_replicas()
    deploy({})


def wait_ready(base_url: str = DEFAULT_BASE_URL, *, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/health/ready", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise TimeoutError(f"{base_url} did not become ready within {timeout}s")


def wait_until_served_only_by(
    base_url: str, replicas: set[str], *, timeout: float = 30, probes: int = 12
) -> None:
    """Wait until NGINX routes only to `replicas` (after it re-resolved Docker DNS)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        seen = served_by(base_url, probes)
        if seen and set(seen) == replicas:
            return
        time.sleep(1)
    raise TimeoutError(f"NGINX still not routing only to {replicas}")


def served_by(base_url: str, count: int) -> list[str]:
    seen = []
    with httpx.Client(base_url=base_url, timeout=5) as client:
        for _ in range(count):
            try:
                seen.append(client.get("/health/live").headers.get("x-served-by", "?"))
            except httpx.HTTPError:
                seen.append("ERROR")
    return seen


def reset_rate_limits(redis_url: str = HOST_REDIS_URL) -> int:
    """Delete rate-limit buckets (ephemeral state only; never financial data)."""
    client = redis.Redis.from_url(redis_url, socket_timeout=2, socket_connect_timeout=2)
    try:
        keys = list(client.scan_iter(f"{RATE_LIMIT_PREFIX}:*"))
        return client.delete(*keys) if keys else 0
    finally:
        client.close()


def ensure_database(name: str) -> None:
    """Create a separate PostgreSQL database (for stack tests / benchmarks) if missing."""
    from app.core.config import Settings

    settings = Settings()  # type: ignore[call-arg]
    user = settings.postgres_user
    exists = subprocess.run(
        [
            "docker",
            "exec",
            "ledgerflow-postgres",
            "psql",
            "-U",
            user,
            "-d",
            "postgres",
            "-tAc",
            f"SELECT 1 FROM pg_database WHERE datname = '{name}'",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if exists != "1":
        subprocess.run(
            [
                "docker",
                "exec",
                "ledgerflow-postgres",
                "psql",
                "-U",
                user,
                "-d",
                "postgres",
                "-c",
                f'CREATE DATABASE "{name}"',
            ],
            capture_output=True,
            text=True,
            check=True,
        )


def postgres_client_connections(database: str) -> int:
    """Client backends currently connected to `database` (pool usage across replicas)."""
    from app.core.config import Settings

    settings = Settings()  # type: ignore[call-arg]
    out = subprocess.run(
        [
            "docker",
            "exec",
            "ledgerflow-postgres",
            "psql",
            "-U",
            settings.postgres_user,
            "-d",
            "postgres",
            "-tAc",
            "SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend' "
            f"AND datname = '{database}'",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return int(out or 0)
