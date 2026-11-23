import type { ComponentHealth, EventLifecycle, EventSummary, PipelineSummary, TopologySnapshot } from "@/types/api";

const NOW = new Date().toISOString();

export function component(overrides: Partial<ComponentHealth> & Pick<ComponentHealth, "id" | "kind">): ComponentHealth {
  return {
    name: overrides.id,
    status: "healthy",
    evidence: "probe",
    source: "probed by api-1",
    message: "Answered.",
    observed_at: NOW,
    age_seconds: 0,
    latency_ms: 1.2,
    last_ok_at: NOW,
    last_error: null,
    last_error_at: null,
    metrics: [],
    ...overrides,
  };
}

export function snapshot(components: ComponentHealth[]): TopologySnapshot {
  return { observed_at: NOW, observed_by: "api-1", tracing_enabled: false, trace_viewer_configured: false, components };
}

export const HEALTHY_STACK: ComponentHealth[] = [
  component({ id: "nginx", kind: "gateway", name: "NGINX gateway", evidence: "request", latency_ms: null }),
  component({ id: "api-1", kind: "api" }),
  component({ id: "api-2", kind: "api" }),
  component({ id: "redis", kind: "cache", name: "Redis" }),
  component({ id: "postgres", kind: "database", name: "PostgreSQL" }),
  component({ id: "outbox-publisher", kind: "worker", name: "Outbox publisher", evidence: "heartbeat", latency_ms: null }),
  component({ id: "rabbitmq", kind: "broker", name: "RabbitMQ", evidence: "derived", latency_ms: null }),
  component({ id: "event-consumer", kind: "worker", name: "Event consumer", evidence: "heartbeat", latency_ms: null }),
  component({ id: "anomaly-detector", kind: "worker", name: "AI anomaly worker", status: "not_configured", evidence: "none", latency_ms: null, observed_at: null, last_ok_at: null }),
  component({ id: "jaeger", kind: "telemetry", name: "Jaeger (traces)", status: "not_configured", evidence: "none", latency_ms: null, observed_at: null, last_ok_at: null }),
];

export const TRANSFER_ID = "22222222-2222-4222-8222-222222222222";
export const EVENT_ID = "33333333-3333-4333-8333-333333333333";

export function eventSummary(overrides: Partial<EventSummary> = {}): EventSummary {
  return {
    id: EVENT_ID,
    event_type: "transfer.completed",
    event_version: 1,
    aggregate_id: TRANSFER_ID,
    transfer_id: TRANSFER_ID,
    amount_minor: 1234,
    correlation_id: "req-1",
    trace_id: null,
    status: "published",
    attempts: 1,
    last_error: null,
    created_at: "2026-11-20T10:00:00.000Z",
    available_at: "2026-11-20T10:00:00.000Z",
    published_at: "2026-11-20T10:00:00.300Z",
    consumers: [{ consumer: "ledger-audit", processed_at: "2026-11-20T10:00:00.350Z", delivery_attempt: 1 }],
    ...overrides,
  };
}

export function lifecycle(overrides: Partial<EventLifecycle> = {}): EventLifecycle {
  return {
    transfer_id: TRANSFER_ID,
    transfer_kind: "transfer",
    transfer_created_at: "2026-11-20T10:00:00.000Z",
    event: eventSummary(),
    expected_consumers: ["ledger-audit"],
    stages: [
      { id: "commit", label: "Transaction committed", state: "observed", at: "2026-11-20T10:00:00.000Z", detail: "Committed.", duration_ms: null },
      { id: "outbox", label: "Outbox row written", state: "observed", at: "2026-11-20T10:00:00.000Z", detail: "Same transaction.", duration_ms: null },
      { id: "publish", label: "Published to RabbitMQ", state: "observed", at: "2026-11-20T10:00:00.300Z", detail: "Broker confirmed after 1 publish attempt(s).", duration_ms: 300 },
      { id: "consume:ledger-audit", label: "Consumed by ledger-audit", state: "observed", at: "2026-11-20T10:00:00.350Z", detail: "Receipt and effect committed together.", duration_ms: 50 },
    ],
    ...overrides,
  };
}

export function pipelineSummary(overrides: Partial<PipelineSummary> = {}): PipelineSummary {
  return {
    observed_at: NOW,
    outbox_pending: 0,
    outbox_processing: 0,
    outbox_published: 42,
    awaiting_retry: 0,
    republished_events: 1,
    oldest_unpublished_at: null,
    oldest_unpublished_age_seconds: null,
    published_last_5m: 5,
    publish_lag_ms_p50: 280,
    publish_lag_ms_p95: 510,
    publisher_last_error: null,
    publisher_last_error_at: null,
    consumers: [
      {
        consumer: "ledger-audit",
        component: "event-consumer",
        events_processed: 42,
        last_processed_at: NOW,
        effects_after_redelivery: 0,
        since_start: { processed: 10, duplicate: 1, retry_scheduled: 0, dead_lettered: 0 },
        queues: [
          { queue: "ledgerflow.ledger-audit", role: "main", ready: 0, consumers: 1 },
          { queue: "ledgerflow.ledger-audit.dlq", role: "dead_letter", ready: 0, consumers: 0 },
        ],
        reported_at: NOW,
        report_stale: false,
      },
    ],
    ...overrides,
  };
}
