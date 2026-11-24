"""Live topology: the health of every component, each with its evidence (Phase 5C).

Read-only. Nothing here controls a container or asks Docker anything: a status comes from
what the application itself can observe.

  NGINX          the snapshot request came through it (X-Request-ID it stamps)
  API replicas   HTTP readiness probe of api-1 .. api-N on the internal network
  PostgreSQL     a query from this replica (latency, connections)
  Redis          PING + INFO from this replica (the rate limiter's own client)
  workers        their own heartbeat rows; stale when past their announced deadline
  RabbitMQ       probe (when configured, Phase 6A): management-API health check and queue
                 depths (incl. dead-letter queues) read with a monitoring-only user whose
                 permissions match no resource; the API never speaks AMQP and never holds
                 broker admin credentials. Without the probe: derived from what the workers
                 report about their own broker connection.
  Jaeger         HTTP probe of its query API, when configured

Probes run in parallel with short timeouts, so one dead dependency costs about a second,
not a hang. "Last OK" for probed components is what THIS replica has seen since it
started (it is process memory, labelled as such).
"""

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote

import httpx
import redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.observability.schemas import (
    ComponentHealth,
    ComponentMetric,
    ComponentStatus,
    TopologySnapshot,
)

PROBE_TIMEOUT = httpx.Timeout(1.5, connect=0.5)
WORKER_NAMES = {
    "outbox-publisher": "Outbox publisher",
    "event-consumer": "Event consumer",
    "anomaly-detector": "AI anomaly worker",
}


@dataclass(frozen=True, slots=True)
class BrokerQueue:
    name: str
    role: Literal["main", "retry", "dead_letter"]
    ready: int
    unacked: int


@dataclass(frozen=True, slots=True)
class BrokerProbe:
    """What the independent management-API probe saw (Phase 6A)."""

    state: Literal["ok", "alarm", "auth", "down"]
    at: datetime
    latency_ms: float | None = None
    queues: list[BrokerQueue] = field(default_factory=list)
    error: str | None = None

    @property
    def dead_lettered(self) -> int:
        return sum(q.ready + q.unacked for q in self.queues if q.role == "dead_letter")


def _queue_role(name: str) -> Literal["main", "retry", "dead_letter"]:
    if name.endswith(".dlq"):
        return "dead_letter"
    return "retry" if ".retry." in name else "main"


def _now() -> datetime:
    return datetime.now(UTC)


def _age(at: datetime | None, now: datetime) -> float | None:
    return None if at is None else round(max(0.0, (now - at).total_seconds()), 1)


def _metric(key: str, label: str, value: Any, unit: str | None = None) -> ComponentMetric:
    return ComponentMetric(key=key, label=label, value=value, unit=unit)


class TopologyProbe:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: sessionmaker[Session],
        redis_client: redis.Redis | None,
        tracing_enabled: bool,
        http_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._sessions = session_factory
        self._redis = redis_client
        self._tracing = tracing_enabled
        self._http = httpx.Client(timeout=PROBE_TIMEOUT, transport=http_transport)
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="topology-probe")
        self._last_ok: dict[str, datetime] = {}
        self._lock = threading.Lock()

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._http.close()

    @property
    def replica_names(self) -> list[str]:
        count = self._settings.observability_upstream_count
        return [f"api-{i}" for i in range(1, count + 1)]

    # --- snapshot -----------------------------------------------------------------------

    def snapshot(self, *, request_id: str | None) -> TopologySnapshot:
        jobs: list[Callable[[], list[ComponentHealth]]] = [
            lambda: [self._postgres()],
            lambda: [self._redis_health()],
            self._workers_and_broker,
            lambda: [self._jaeger()],
        ]
        if self.replica_names:
            jobs.extend((lambda name=name: [self._replica(name)]) for name in self.replica_names)
        else:
            jobs.append(lambda: [self._self_replica()])
        futures = [self._pool.submit(job) for job in jobs]
        components = [self._nginx(request_id)]
        for future in futures:
            components.extend(future.result())
        order = ["gateway", "api", "cache", "database", "worker", "broker", "telemetry"]
        components.sort(key=lambda c: (order.index(c.kind), c.id))
        return TopologySnapshot(
            observed_at=_now(),
            observed_by=self._settings.replica_id,
            tracing_enabled=self._tracing,
            trace_viewer_configured=self._settings.trace_store_url is not None,
            components=components,
        )

    def _ok(self, component_id: str, at: datetime) -> datetime:
        with self._lock:
            self._last_ok[component_id] = at
            return at

    def _previous_ok(self, component_id: str) -> datetime | None:
        with self._lock:
            return self._last_ok.get(component_id)

    def _probed(
        self,
        component_id: str,
        *,
        name: str,
        kind: str,
        status: ComponentStatus,
        message: str,
        at: datetime,
        latency_ms: float | None,
        metrics: list[ComponentMetric] | None = None,
        error: str | None = None,
        source: str | None = None,
    ) -> ComponentHealth:
        if status == "healthy":
            last_ok: datetime | None = self._ok(component_id, at)
        else:
            last_ok = self._previous_ok(component_id)
        return ComponentHealth(
            id=component_id,
            name=name,
            kind=kind,  # type: ignore[arg-type]
            status=status,
            evidence="probe",
            source=source or f"probed by {self._settings.replica_id}",
            message=message,
            observed_at=at,
            age_seconds=0.0,
            latency_ms=None if latency_ms is None else round(latency_ms, 1),
            last_ok_at=last_ok,
            last_error=error,
            last_error_at=at if error else None,
            metrics=metrics or [],
        )

    # --- individual components -------------------------------------------------------------

    def _nginx(self, request_id: str | None) -> ComponentHealth:
        now = _now()
        upstreams = self._settings.observability_upstream_count
        metrics = [_metric("upstreams", "Upstream replicas", upstreams)] if upstreams else []
        if request_id:
            return ComponentHealth(
                id="nginx",
                name="NGINX gateway",
                kind="gateway",
                status="healthy",
                evidence="request",
                source="this snapshot request passed through it",
                message="Forwarded this request (it stamped X-Request-ID).",
                observed_at=now,
                age_seconds=0.0,
                last_ok_at=now,
                metrics=metrics,
            )
        return ComponentHealth(
            id="nginx",
            name="NGINX gateway",
            kind="gateway",
            status="unknown",
            evidence="none",
            source="no X-Request-ID on this request",
            message="This request did not come through NGINX, so its state was not observed.",
            observed_at=None,
            age_seconds=None,
            metrics=metrics,
        )

    def _replica(self, name: str) -> ComponentHealth:
        url = f"http://{name}:{self._settings.observability_replica_port}/health/ready"
        started = time.perf_counter()
        at = _now()
        try:
            response = self._http.get(url)
        except httpx.ConnectTimeout:
            return self._replica_down(name, at, "No answer within 0.5 s (stopped or unreachable).")
        except httpx.ConnectError:
            return self._replica_down(name, at, "Connection refused or host unreachable.")
        except httpx.TimeoutException:
            return self._replica_down(name, at, "Accepted the connection but did not answer.")
        except httpx.HTTPError as exc:
            return self._replica_down(name, at, f"Probe failed: {type(exc).__name__}")
        latency = (time.perf_counter() - started) * 1000
        try:
            body = response.json()
        except ValueError:
            body = {}
        deps = {k: body.get(k) for k in ("database", "redis") if k in body}
        metrics = [_metric(k, k.capitalize(), v) for k, v in deps.items()]
        if response.status_code == 200:
            return self._probed(
                name,
                name=name,
                kind="api",
                status="healthy",
                message="Ready: PostgreSQL and Redis reachable from this replica.",
                at=at,
                latency_ms=latency,
                metrics=metrics,
            )
        failing = [k for k, v in deps.items() if v == "unavailable"]
        return self._probed(
            name,
            name=name,
            kind="api",
            status="degraded",
            message=(
                f"Process is up but not ready: {', '.join(failing)} unavailable."
                if failing
                else f"Readiness check answered {response.status_code}."
            ),
            at=at,
            latency_ms=latency,
            metrics=metrics,
            error=f"readiness {response.status_code}: {', '.join(failing) or 'not ready'}",
        )

    def _replica_down(self, name: str, at: datetime, message: str) -> ComponentHealth:
        return self._probed(
            name,
            name=name,
            kind="api",
            status="unhealthy",
            message=message,
            at=at,
            latency_ms=None,
            error=message,
        )

    def _self_replica(self) -> ComponentHealth:
        at = _now()
        name = self._settings.replica_id
        return self._probed(
            name,
            name=name,
            kind="api",
            status="healthy",
            message="This process answered the snapshot request (single local process).",
            at=at,
            latency_ms=None,
            source="self (no replica list configured)",
        )

    def _postgres(self) -> ComponentHealth:
        at = _now()
        started = time.perf_counter()
        try:
            with self._sessions() as session:
                session.execute(text("SELECT 1"))
                latency = (time.perf_counter() - started) * 1000
                row = session.execute(
                    text(
                        "SELECT (SELECT count(*) FROM pg_stat_activity "
                        "        WHERE datname = current_database()) AS connections, "
                        "current_setting('max_connections')::int AS max_connections, "
                        "current_setting('server_version') AS version"
                    )
                ).one()
        except SQLAlchemyError as exc:
            message = "Not reachable from this replica."
            return self._probed(
                "postgres",
                name="PostgreSQL",
                kind="database",
                status="unhealthy",
                message=message,
                at=at,
                latency_ms=None,
                error=f"{type(exc).__name__}: {getattr(exc, 'orig', None) or exc}"[:300],
            )
        return self._probed(
            "postgres",
            name="PostgreSQL",
            kind="database",
            status="healthy",
            message="Answered a query from this replica.",
            at=at,
            latency_ms=latency,
            metrics=[
                _metric("connections", "Connections (this database)", row.connections),
                _metric("max_connections", "max_connections", row.max_connections),
                _metric("version", "Version", row.version.split(" ")[0]),
            ],
        )

    def _redis_health(self) -> ComponentHealth:
        at = _now()
        if self._redis is None:
            return ComponentHealth(
                id="redis",
                name="Redis",
                kind="cache",
                status="not_configured",
                evidence="none",
                source="RATE_LIMIT_BACKEND is not redis",
                message="This process uses the in-memory limiter, not Redis.",
                observed_at=None,
                age_seconds=None,
            )
        started = time.perf_counter()
        try:
            self._redis.ping()
            latency = (time.perf_counter() - started) * 1000
            info = self._redis.info()
        except RedisError as exc:
            return self._probed(
                "redis",
                name="Redis",
                kind="cache",
                status="unhealthy",
                message="Not reachable: financial writes fail closed (503), reads fail open.",
                at=at,
                latency_ms=None,
                error=f"{type(exc).__name__}: {exc}"[:300],
            )
        return self._probed(
            "redis",
            name="Redis",
            kind="cache",
            status="healthy",
            message="Answered PING from the rate limiter's connection pool.",
            at=at,
            latency_ms=latency,
            metrics=[
                _metric("clients", "Connected clients", info.get("connected_clients")),
                _metric("memory", "Memory used", info.get("used_memory_human")),
                _metric("uptime", "Uptime", info.get("uptime_in_seconds"), "s"),
            ],
        )

    def _jaeger(self) -> ComponentHealth:
        url = self._settings.trace_store_url
        if url is None:
            return ComponentHealth(
                id="jaeger",
                name="Jaeger (traces)",
                kind="telemetry",
                status="not_configured",
                evidence="none",
                source="tracing is off on this replica",
                message="Optional: OTEL_ENABLED=true and docker compose --profile observability.",
                observed_at=None,
                age_seconds=None,
            )
        at = _now()
        started = time.perf_counter()
        try:
            response = self._http.get(f"{url}/api/services")
            response.raise_for_status()
            services = response.json().get("data") or []
        except (httpx.HTTPError, ValueError) as exc:
            return self._probed(
                "jaeger",
                name="Jaeger (traces)",
                kind="telemetry",
                status="unhealthy" if self._tracing else "unknown",
                message=(
                    "Trace store not reachable; spans are dropped, requests are unaffected."
                    if self._tracing
                    else "Not running (tracing is off on this replica)."
                ),
                at=at,
                latency_ms=None,
                error=f"{type(exc).__name__}"[:300],
            )
        return self._probed(
            "jaeger",
            name="Jaeger (traces)",
            kind="telemetry",
            status="healthy" if self._tracing else "degraded",
            message=(
                "Receiving spans."
                if self._tracing
                else "Running, but this replica has tracing off (OTEL_ENABLED=false)."
            ),
            at=at,
            latency_ms=(time.perf_counter() - started) * 1000,
            metrics=[_metric("services", "Services reporting", len(services))],
        )

    # --- workers (heartbeats) and the broker (derived) ---------------------------------------

    def _workers_and_broker(self) -> list[ComponentHealth]:
        try:
            with self._sessions() as session:
                rows = list(
                    session.execute(
                        text(
                            "SELECT component, instance, status, details, last_error, "
                            "last_error_at, started_at, beat_at, next_beat_before, "
                            "now() AS db_now FROM worker_heartbeats "
                            "WHERE beat_at > now() - interval '1 day' ORDER BY beat_at DESC"
                        )
                    ).mappings()
                )
        except SQLAlchemyError:
            rows = None
        expected = self._settings.observability_expected_workers
        components = [self._worker(component, rows, expected) for component in WORKER_NAMES]
        probe = self.probe_broker()
        components.append(self._broker(rows) if probe is None else self._broker_probed(rows, probe))
        return components

    def probe_broker(self) -> BrokerProbe | None:
        """Independent, read-only broker check through the management API (or None).

        Uses the monitoring-only user: it can read health checks and queue depths, and its
        vhost permissions match no resource ("^$"), so this replica cannot declare,
        publish, consume or change anything on the broker.
        """
        url = self._settings.rabbitmq_management_url
        password = self._settings.rabbitmq_monitor_password
        if url is None or password is None:
            return None
        auth = (self._settings.rabbitmq_monitor_user, password.get_secret_value())
        at = _now()
        started = time.perf_counter()
        try:
            alarms = self._http.get(f"{url}/api/health/checks/alarms", auth=auth)
            latency = (time.perf_counter() - started) * 1000
            if alarms.status_code in (401, 403):
                return BrokerProbe("auth", at, latency, error=f"HTTP {alarms.status_code}")
            vhost = quote(self._settings.rabbitmq_vhost, safe="")
            response = self._http.get(
                f"{url}/api/queues/{vhost}",
                params={"columns": "name,messages_ready,messages_unacknowledged"},
                auth=auth,
            )
            rows = response.json() if response.status_code == 200 else []
        except (httpx.HTTPError, ValueError) as exc:
            return BrokerProbe("down", at, error=type(exc).__name__)
        prefix = f"{self._settings.events_topology_prefix}."
        queues = sorted(
            (
                BrokerQueue(
                    name=row["name"],
                    role=_queue_role(row["name"]),
                    ready=int(row.get("messages_ready") or 0),
                    unacked=int(row.get("messages_unacknowledged") or 0),
                )
                for row in (rows if isinstance(rows, list) else [])
                if isinstance(row, dict) and str(row.get("name", "")).startswith(prefix)
            ),
            key=lambda q: q.name,
        )
        if alarms.status_code == 200:
            return BrokerProbe("ok", at, latency, queues)
        if alarms.status_code == 503:
            return BrokerProbe("alarm", at, latency, queues, error="resource alarm in effect")
        return BrokerProbe("down", at, latency, error=f"health check HTTP {alarms.status_code}")

    def _broker_probed(self, rows: list[Any] | None, probe: BrokerProbe) -> ComponentHealth:
        fresh = [
            r
            for r in rows or []
            if r["next_beat_before"] >= r["db_now"]
            and (r["details"] or {}).get("broker_connected") is not None
        ]
        connected = sum(1 for r in fresh if r["details"]["broker_connected"])
        metrics = [_metric("workers_connected", "Workers connected", f"{connected}/{len(fresh)}")]
        if probe.queues:
            metrics.append(
                _metric("dead_lettered", "Dead-lettered (all DLQs)", probe.dead_lettered)
            )
            metrics.extend(
                _metric(f"queue:{q.name}", f"{q.name} ready", q.ready) for q in probe.queues
            )
        source = f"management API, probed by {self._settings.replica_id} (monitoring user)"
        dlq_note = (
            f" {probe.dead_lettered} dead-lettered message(s) await inspection."
            if probe.dead_lettered
            else ""
        )
        if probe.state == "auth":
            return ComponentHealth(
                id="rabbitmq",
                name="RabbitMQ",
                kind="broker",
                status="unknown",
                evidence="none",
                source=source,
                message=(
                    "The monitoring credentials were rejected; run the rabbitmq-monitor-setup "
                    "job (docker compose up rabbitmq-monitor-setup)."
                ),
                observed_at=probe.at,
                age_seconds=0.0,
                last_error=probe.error,
                last_error_at=probe.at,
                metrics=metrics,
            )
        status: ComponentStatus
        if probe.state == "ok":
            status = "healthy"
            message = "Health check passed (no resource alarms)." + dlq_note
        elif probe.state == "alarm":
            status = "degraded"
            message = (
                "A memory or disk alarm is in effect: publishers are blocked; transfers still "
                "commit and events wait in the outbox." + dlq_note
            )
        else:
            status = "unhealthy"
            message = (
                "Management API not reachable: the broker is down or unreachable. Transfers "
                "still commit; events wait in the outbox."
            )
        return self._probed(
            "rabbitmq",
            name="RabbitMQ",
            kind="broker",
            status=status,
            message=message,
            at=probe.at,
            latency_ms=probe.latency_ms,
            metrics=metrics,
            error=probe.error,
            source=source,
        )

    def _worker(
        self, component: str, rows: list[Any] | None, expected: list[str]
    ) -> ComponentHealth:
        name = WORKER_NAMES[component]
        if rows is None:
            return ComponentHealth(
                id=component,
                name=name,
                kind="worker",
                status="unknown",
                evidence="none",
                source="heartbeats unreadable",
                message="PostgreSQL is unreachable, so worker heartbeats cannot be read.",
                observed_at=None,
                age_seconds=None,
            )
        mine = [r for r in rows if r["component"] == component]
        if not mine:
            configured = component in expected
            return ComponentHealth(
                id=component,
                name=name,
                kind="worker",
                status="unknown" if configured else "not_configured",
                evidence="none",
                source="no heartbeat in the last 24 h",
                message=(
                    "No heartbeat recorded: the worker is not running or cannot reach PostgreSQL."
                    if configured
                    else "Not part of this deployment."
                ),
                observed_at=None,
                age_seconds=None,
            )
        row = mine[0]
        db_now: datetime = row["db_now"]
        fresh = [r for r in mine if r["next_beat_before"] >= db_now]
        age = _age(row["beat_at"], db_now)
        details: dict[str, Any] = row["details"] or {}
        if row["next_beat_before"] < db_now:
            status: ComponentStatus = "stale"
            message = f"Last heartbeat {age:.0f} s ago, past its deadline: not running or stuck."
        else:
            status = {"ok": "healthy", "degraded": "degraded", "error": "unhealthy"}[row["status"]]
            message = (
                details.get("summary")
                or {
                    "healthy": "Main loop running and reporting.",
                    "degraded": "Running, but cannot make progress.",
                    "unhealthy": "Reporting an error.",
                }[status]
            )
        last_ok = details.get("last_ok_at")
        return ComponentHealth(
            id=component,
            name=name,
            kind="worker",
            status=status,
            evidence="heartbeat",
            source=f"heartbeat from {row['instance']}",
            message=message,
            observed_at=row["beat_at"],
            age_seconds=age,
            last_ok_at=datetime.fromisoformat(last_ok) if last_ok else None,
            last_error=row["last_error"],
            last_error_at=row["last_error_at"],
            metrics=[
                *(
                    _metric(m["key"], m["label"], m["value"], m.get("unit"))
                    for m in details.get("metrics", [])
                ),
                _metric("instances", "Instances reporting", len(fresh)),
            ],
        )

    def _broker(self, rows: list[Any] | None) -> ComponentHealth:
        name = "RabbitMQ"
        fresh = [
            r
            for r in rows or []
            if r["next_beat_before"] >= r["db_now"]
            and (r["details"] or {}).get("broker_connected") is not None
        ]
        if not fresh:
            return ComponentHealth(
                id="rabbitmq",
                name=name,
                kind="broker",
                status="unknown",
                evidence="none",
                source="no fresh worker report",
                message="The API never connects to the broker; no worker reported on it recently.",
                observed_at=None,
                age_seconds=None,
            )
        connected = [r for r in fresh if r["details"]["broker_connected"]]
        reporter = (connected or fresh)[0]
        db_now = reporter["db_now"]
        queues = next(
            (r["details"].get("queues") for r in fresh if r["details"].get("queues")), None
        )
        metrics = []
        for queue in queues or []:
            metrics.append(
                _metric(f"queue:{queue['queue']}", f"{queue['role']} queue ready", queue["ready"])
            )
        if connected:
            return ComponentHealth(
                id="rabbitmq",
                name=name,
                kind="broker",
                status="healthy",
                evidence="derived",
                source=f"connection of {reporter['component']} ({reporter['instance']})",
                message="Workers hold an open broker connection.",
                observed_at=reporter["beat_at"],
                age_seconds=_age(reporter["beat_at"], db_now),
                last_ok_at=reporter["beat_at"],
                metrics=metrics,
            )
        return ComponentHealth(
            id="rabbitmq",
            name=name,
            kind="broker",
            status="unhealthy",
            evidence="derived",
            source=f"reported by {', '.join(sorted({r['component'] for r in fresh}))}",
            message="Workers cannot connect. Transfers still commit; events wait in the outbox.",
            observed_at=reporter["beat_at"],
            age_seconds=_age(reporter["beat_at"], db_now),
            last_ok_at=max(
                (
                    datetime.fromisoformat(r["details"]["broker_ok_at"])
                    for r in fresh
                    if r["details"].get("broker_ok_at")
                ),
                default=None,
            ),
            last_error=reporter["last_error"],
            last_error_at=reporter["last_error_at"],
            metrics=metrics,
        )
