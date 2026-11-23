"""Read-only observability contract (Phase 5C): live topology, event pipeline, traces.

Every status carries its evidence (how it was obtained) and the time of that evidence, so
the UI can distinguish a fresh observation from an old one and never shows a guess as a
measurement.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

ComponentStatus = Literal["healthy", "degraded", "unhealthy", "stale", "unknown", "not_configured"]
ComponentKind = Literal["gateway", "api", "database", "cache", "worker", "broker", "telemetry"]
# probe:     this replica just checked it (connection, query, HTTP readiness call)
# heartbeat: the component reported on itself (worker_heartbeats row)
# derived:   reported by a component that talks to it (e.g. RabbitMQ via the workers)
# request:   inferred from the snapshot request itself (it came through NGINX)
# none:      no evidence at all
Evidence = Literal["probe", "heartbeat", "derived", "request", "none"]


class ComponentMetric(BaseModel):
    key: str
    label: str
    value: int | float | str | bool | None
    unit: str | None = None


class ComponentHealth(BaseModel):
    id: str
    name: str
    kind: ComponentKind
    status: ComponentStatus
    evidence: Evidence
    source: str
    message: str
    observed_at: datetime | None
    age_seconds: float | None
    latency_ms: float | None = None
    last_ok_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    metrics: list[ComponentMetric] = []


class TopologySnapshot(BaseModel):
    observed_at: datetime
    observed_by: str
    tracing_enabled: bool
    trace_viewer_configured: bool
    components: list[ComponentHealth]


# --- event pipeline -----------------------------------------------------------------------

OutboxStatus = Literal["pending", "processing", "published"]
StageState = Literal["observed", "pending", "failed", "not_applicable", "unknown"]


class QueueDepth(BaseModel):
    queue: str
    role: Literal["main", "retry", "dead_letter"]
    ready: int
    unacked: int | None = None
    consumers: int | None = None


class ConsumerStats(BaseModel):
    consumer: str
    component: str
    events_processed: int
    last_processed_at: datetime | None
    effects_after_redelivery: int | None
    since_start: dict[str, int] | None
    queues: list[QueueDepth]
    reported_at: datetime | None
    report_stale: bool | None


class PipelineSummary(BaseModel):
    observed_at: datetime
    outbox_pending: int
    outbox_processing: int
    outbox_published: int
    awaiting_retry: int
    republished_events: int
    oldest_unpublished_at: datetime | None
    oldest_unpublished_age_seconds: float | None
    published_last_5m: int
    publish_lag_ms_p50: float | None
    publish_lag_ms_p95: float | None
    publisher_last_error: str | None
    publisher_last_error_at: datetime | None
    consumers: list[ConsumerStats]


class EventConsumption(BaseModel):
    consumer: str
    processed_at: datetime
    delivery_attempt: int | None = None


class EventSummary(BaseModel):
    id: str
    event_type: str
    event_version: int
    aggregate_id: str
    transfer_id: str | None
    amount_minor: int | None
    correlation_id: str | None
    trace_id: str | None
    status: OutboxStatus
    attempts: int
    last_error: str | None
    created_at: datetime
    available_at: datetime
    published_at: datetime | None
    consumers: list[EventConsumption]


class EventPage(BaseModel):
    items: list[EventSummary]
    next_cursor: str | None


class LifecycleStage(BaseModel):
    id: str
    label: str
    state: StageState
    at: datetime | None = None
    detail: str
    duration_ms: float | None = None


class EventLifecycle(BaseModel):
    """Persisted evidence for one money movement's asynchronous path."""

    transfer_id: str | None
    transfer_kind: str | None
    transfer_created_at: datetime | None
    event: EventSummary | None
    expected_consumers: list[str]
    stages: list[LifecycleStage]


# --- traces -------------------------------------------------------------------------------


class TraceSpan(BaseModel):
    span_id: str
    parent_id: str | None
    name: str
    service: str
    instance: str | None
    start_offset_ms: float
    duration_ms: float
    error: bool
    attributes: dict[str, str | int | float | bool]


class TraceView(BaseModel):
    trace_id: str
    available: bool
    reason: str | None = None
    spans: list[TraceSpan] = []
    services: list[str] = []
    truncated: bool = False
