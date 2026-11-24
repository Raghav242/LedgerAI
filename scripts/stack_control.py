"""Control the LOCAL Docker Compose stack (used by demos, stack tests and benchmarks).

Every action that stops, starts or reconfigures containers first checks that the target
API is on this machine; failure simulations are local-only by construction.

Three Compose projects are known, all built from the same docker-compose.yml:
  DEV    `ledgerflow`            the development stack (the demos act on it)
  BENCH  `ledgerflow-bench`      benchmarks (Phase 6A): gateway, replicas, PostgreSQL, Redis
  STACK  `ledgerflow-stacktest`  stack tests (Phase 6B): the complete stack incl. broker,
                                 workers and AI worker
BENCH and STACK each have their own containers, network, volumes (so their own PostgreSQL
server), loopback ports and database, are created fresh and removed afterwards (volumes
included). Neither ever touches DEV; assert_isolated() refuses any overlap.
"""

import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx
import redis

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_URL = "http://127.0.0.1:8080"
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
RATE_LIMIT_PREFIX = "ledgerflow:rl"
HOST_REDIS_URL = "redis://127.0.0.1:6379/0"  # the stack's Redis, published on loopback


@dataclass(frozen=True)
class Project:
    """One Compose project built from docker-compose.yml."""

    name: str
    base_url: str
    redis_url: str
    container_prefix: str
    # Interpolation variables that make this project independent of every other one.
    env: dict[str, str] = field(default_factory=dict)
    # Services `up` starts; empty = every service in the file.
    services: tuple[str, ...] = ()

    def container(self, service: str) -> str:
        """Services with a fixed container_name (postgres, redis, rabbitmq, nginx, jaeger)."""
        return f"{self.container_prefix}-{service}"

    def service_container(self, service: str, index: int = 1) -> str:
        """Compose-named containers (api-N, workers): <project>-<service>-<index>."""
        return f"{self.name}-{service}-{index}"

    @property
    def network(self) -> str:
        return f"{self.name}_backend"


DEV = Project(
    name="ledgerflow",
    base_url=DEFAULT_BASE_URL,
    redis_url=HOST_REDIS_URL,
    container_prefix="ledgerflow",
)

BENCH_DB = "ledgerflow_bench"
_BENCH_NET = os.environ.get("LEDGERFLOW_BENCH_NET", "172.29.0")
BENCH = Project(
    name="ledgerflow-bench",
    base_url="http://127.0.0.1:8180",
    redis_url="redis://127.0.0.1:6479/0",
    container_prefix="ledgerflow-bench",
    env={
        "STACK_CONTAINER_PREFIX": "ledgerflow-bench",
        "BACKEND_NET": _BENCH_NET,
        "NGINX_BIND": "127.0.0.1",
        "NGINX_PORT": "8180",
        "POSTGRES_PORT": "5532",
        "REDIS_PORT": "6479",
        "RABBITMQ_PORT": "5772",
        "RABBITMQ_MANAGEMENT_PORT": "15772",
        "JAEGER_UI_PORT": "16786",
        "FRONTEND_PORT": "3100",
        # Its own PostgreSQL server (project-scoped volume) AND a distinct database name.
        "POSTGRES_DB": BENCH_DB,
        "API_POSTGRES_DB": BENCH_DB,
    },
    # Gateway + replicas + their dependencies (migrate, postgres, redis). No broker or
    # event workers: they are not part of what the benchmark measures, and leaving them
    # out keeps a second stack affordable on a laptop (the outbox simply accumulates in
    # the throw-away benchmark database).
    services=("nginx", "api-1", "api-2"),
)

STACK_DB = "ledgerflow_stacktest"
_STACK_NET = os.environ.get("LEDGERFLOW_STACKTEST_NET", "172.30.0")
STACK = Project(
    name="ledgerflow-stacktest",
    base_url="http://127.0.0.1:8280",
    redis_url="redis://127.0.0.1:6579/0",
    container_prefix="ledgerflow-stacktest",
    env={
        "STACK_CONTAINER_PREFIX": "ledgerflow-stacktest",
        "BACKEND_NET": _STACK_NET,
        "NGINX_BIND": "127.0.0.1",
        "NGINX_PORT": "8280",
        "POSTGRES_PORT": "5632",
        "REDIS_PORT": "6579",
        "RABBITMQ_PORT": "5872",
        "RABBITMQ_MANAGEMENT_PORT": "15872",
        "JAEGER_UI_PORT": "16886",
        "FRONTEND_PORT": "3200",
        "POSTGRES_DB": STACK_DB,
        "API_POSTGRES_DB": STACK_DB,
    },
    services=(),  # everything: the stack tests exercise broker, workers and AI worker
)

# What makes two projects collide if equal. assert_isolated() compares all of them.
_ISOLATION_KEYS = (
    "STACK_CONTAINER_PREFIX",
    "BACKEND_NET",
    "NGINX_PORT",
    "POSTGRES_PORT",
    "REDIS_PORT",
    "RABBITMQ_PORT",
    "RABBITMQ_MANAGEMENT_PORT",
    "JAEGER_UI_PORT",
    "FRONTEND_PORT",
)
_DEV_DEFAULTS = {
    "STACK_CONTAINER_PREFIX": "ledgerflow",
    "BACKEND_NET": "172.28.0",
    "NGINX_PORT": "8080",
    "POSTGRES_PORT": "5432",
    "REDIS_PORT": "6379",
    "RABBITMQ_PORT": "5672",
    "RABBITMQ_MANAGEMENT_PORT": "15672",
    "JAEGER_UI_PORT": "16686",
    "FRONTEND_PORT": "3000",
}


def assert_isolated(project: Project, dev: Project = DEV) -> None:
    """Refuse to use `project` unless nothing about it can reach `dev` (the DEV stack)."""
    problems = []
    if project.name == dev.name:
        problems.append(f"same Compose project name {project.name!r}")
    if project.container_prefix == dev.container_prefix:
        problems.append("same container prefix")
    if project.base_url == dev.base_url:
        problems.append("same gateway URL")
    if project.redis_url == dev.redis_url:
        problems.append("same Redis URL")
    dev_env = {**_DEV_DEFAULTS, **dev.env}
    for key in _ISOLATION_KEYS:
        if project.env.get(key, _DEV_DEFAULTS[key]) == dev_env[key]:
            problems.append(f"same {key}={dev_env[key]}")
    if project.env.get("API_POSTGRES_DB") in (None, "", "ledgerflow"):
        problems.append("API_POSTGRES_DB must name a dedicated benchmark database")
    if project.env.get("NGINX_BIND", "127.0.0.1") != "127.0.0.1":
        problems.append("the benchmark gateway must bind 127.0.0.1")
    if problems:
        raise SystemExit(
            f"Refusing to use Compose project {project.name!r}: " + "; ".join(problems)
        )


def ensure_local(base_url: str) -> None:
    host = urlparse(base_url).hostname
    if host not in LOCAL_HOSTS:
        raise SystemExit(f"Refusing to control containers for non-local target {base_url!r}")


def compose_command(project: Project, args: tuple[str, ...]) -> list[str]:
    return ["docker", "compose", "-p", project.name, *args]


def compose_env(project: Project, env: dict[str, str] | None) -> dict[str, str]:
    # Process environment beats .env in Compose interpolation; the project's isolation
    # variables are applied LAST so no override can point it back at another project.
    merged = {**os.environ, **(env or {}), **project.env}
    merged.pop("COMPOSE_PROJECT_NAME", None)
    return merged


def compose(
    *args: str,
    env: dict[str, str] | None = None,
    check: bool = True,
    project: Project = DEV,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        compose_command(project, args),
        cwd=ROOT,
        env=compose_env(project, env),
        check=check,
        capture_output=True,
        text=True,
    )


def deploy(
    overrides: dict[str, str] | None = None,
    *,
    profiles: tuple[str, ...] = (),
    project: Project = DEV,
    services: tuple[str, ...] | None = None,
) -> None:
    """(Re)create the stack with environment overrides; only changed services restart."""
    profile_args = [arg for p in profiles for arg in ("--profile", p)]
    targets = project.services if services is None else services
    # --build: `up` alone reuses an existing image even if the source changed since it was
    # built, so tests and demos could silently exercise stale code. Cached layers make
    # this a few seconds when nothing changed.
    result = compose(
        *profile_args,
        "up",
        "-d",
        "--build",
        "--wait",
        *targets,
        env=overrides,
        check=False,
        project=project,
    )
    if result.returncode != 0:
        raise RuntimeError(f"docker compose up failed:\n{result.stdout}\n{result.stderr}")
    # `--wait` means the containers are healthy, not that NGINX routes to them yet: after
    # an all-at-once recreate it may still hold every replica in fail_timeout. Return only
    # once traffic reaches every running replica.
    wait_until_served_only_by(project.base_url, running_replicas(project), timeout=60)


def running_replicas(project: Project = DEV) -> set[str]:
    running = compose("ps", "--services", "--status", "running", project=project).stdout.split()
    return {service for service in running if service.startswith("api-")}


def remove_extra_replicas(project: Project = DEV) -> None:
    compose("--profile", "scale4", "rm", "-s", "-f", "api-3", "api-4", check=False, project=project)


def teardown(project: Project) -> None:
    """Remove an ISOLATED project entirely, including its own volumes. Never DEV."""
    assert_isolated(project)
    compose(
        "--profile",
        "scale4",
        "--profile",
        "observability",
        "down",
        "-v",
        "--remove-orphans",
        check=False,
        project=project,
    )


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


def reset_rate_limits(redis_url: str) -> int:
    """Delete rate-limit buckets (ephemeral state only; never financial data)."""
    client = redis.Redis.from_url(redis_url, socket_timeout=2, socket_connect_timeout=2)
    try:
        keys = list(client.scan_iter(f"{RATE_LIMIT_PREFIX}:*"))
        return client.delete(*keys) if keys else 0
    finally:
        client.close()


def ensure_database(name: str, project: Project = DEV) -> None:
    """Create a separate PostgreSQL database (for stack tests) if missing."""
    from app.core.config import Settings

    settings = Settings()  # type: ignore[call-arg]
    user = settings.postgres_user
    exists = subprocess.run(
        [
            "docker",
            "exec",
            project.container("postgres"),
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
                project.container("postgres"),
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


def postgres_client_connections(database: str, project: Project = DEV) -> int:
    """Client backends currently connected to `database` (pool usage across replicas)."""
    from app.core.config import Settings

    settings = Settings()  # type: ignore[call-arg]
    out = subprocess.run(
        [
            "docker",
            "exec",
            project.container("postgres"),
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


# --- event pipeline helpers (Phase 4A) ---------------------------------------------------


def db_query(sql: str, params: dict | None = None, *, database: str | None = None) -> list:
    """Read-only query against a stack database (default: the dev database)."""
    from sqlalchemy import create_engine, text

    from app.core.config import Settings

    settings = Settings()  # type: ignore[call-arg]
    url = settings.build_database_url(database or settings.postgres_db)
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            return list(conn.execute(text(sql), params or {}).all())
    finally:
        engine.dispose()


def db_execute(sql: str, params: dict | None = None, *, database: str | None = None) -> int:
    """Write to a stack database in its own committed transaction. Returns rowcount."""
    from sqlalchemy import create_engine, text

    from app.core.config import Settings

    settings = Settings()  # type: ignore[call-arg]
    engine = create_engine(settings.build_database_url(database or settings.postgres_db))
    try:
        with engine.begin() as conn:
            return conn.execute(text(sql), params or {}).rowcount
    finally:
        engine.dispose()


def queue_depths(prefix: str = "ledgerflow") -> dict[str, tuple[int, int]]:
    """{queue: (ready, unacked)} for the stack's queues, via rabbitmqctl in the container."""
    out = subprocess.run(
        [
            "docker",
            "exec",
            "ledgerflow-rabbitmq",
            "rabbitmqctl",
            "-q",
            "list_queues",
            "name",
            "messages_ready",
            "messages_unacknowledged",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    depths = {}
    for line in out.splitlines()[1:]:
        name, ready, unacked = line.split("\t")
        if name.startswith(prefix + "."):
            depths[name] = (int(ready), int(unacked))
    return depths


def wait_until(condition, *, timeout: float = 60, interval: float = 0.5, what: str = "") -> None:  # type: ignore[no-untyped-def]
    """Bounded polling of an observable condition."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(interval)
    raise TimeoutError(f"timed out after {timeout}s waiting for {what or condition}")


def container_restart_count(container: str) -> int:
    out = subprocess.run(
        ["docker", "inspect", "-f", "{{.RestartCount}}", container],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return int(out or 0)
