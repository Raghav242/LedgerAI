/**
 * Static explanations per component: what it is responsible for and the design decisions
 * behind it (from docker-compose.yml and the design notes). Shown in the details panel,
 * always next to the live status, never instead of it.
 */
export interface ComponentInfo {
  short: string;
  responsibility: string;
  decisions: string[];
}

export const COMPONENT_INFO: Record<string, ComponentInfo> = {
  browser: {
    short: "This app",
    responsibility: "The React frontend. Vite proxies /api and /health to NGINX, so the browser makes same-origin requests.",
    decisions: ["No CORS configuration needed on the backend.", "All money is simulated; there is no authentication, so it binds to 127.0.0.1 only."],
  },
  nginx: {
    short: "Gateway",
    responsibility: "The only published API port. Round-robins requests across the replicas and stamps X-Request-ID.",
    decisions: [
      "Retries connection failures on another replica, but never re-sends a POST that may have reached a replica (the client retries with the same Idempotency-Key instead).",
      "Its X-Request-ID becomes the correlation id of the events a request commits.",
    ],
  },
  api: {
    short: "FastAPI replica",
    responsibility: "Stateless request handling: validation, rate limiting, idempotency and the transfer transaction.",
    decisions: [
      "All state lives in PostgreSQL and Redis, so any replica can serve any request, including retries.",
      "Liveness checks only the process; readiness also checks PostgreSQL and Redis (shown here).",
    ],
  },
  redis: {
    short: "Rate limiter",
    responsibility: "Shared token buckets: one atomic Lua script per request, so the quota is global across replicas.",
    decisions: [
      "Writes fail CLOSED (503) when Redis is unreachable; reads fail open.",
      "No persistence: it holds no financial data, and a restart only refills buckets.",
    ],
  },
  postgres: {
    short: "System of record",
    responsibility: "Accounts, transfers, the double-entry ledger, idempotency records, the transactional outbox, receipts and lab runs.",
    decisions: [
      "Row locks in id order prevent overdrafts and deadlocks.",
      "The outbox row commits in the same transaction as the money movement: no dual write.",
    ],
  },
  "outbox-publisher": {
    short: "Event relay",
    responsibility: "Claims committed outbox rows (SKIP LOCKED, leased) and publishes them to RabbitMQ with publisher confirms.",
    decisions: [
      "At-least-once: a crash after the confirm can publish twice; consumers deduplicate by event id.",
      "Health comes from its own heartbeat row, not from the container existing.",
    ],
  },
  rabbitmq: {
    short: "Message broker",
    responsibility: "Durable quorum queues with TTL retry tiers and a dead-letter queue.",
    decisions: [
      "The API never connects to it: a broker outage delays events but never blocks a transfer.",
      "Single node: survives restarts, not disk loss. Its status here is reported by the workers.",
    ],
  },
  "event-consumer": {
    short: "Audit worker",
    responsibility: "Applies each event exactly once: receipt + effect in one transaction, then ack.",
    decisions: ["A redelivered event finds its receipt and is acknowledged without a second effect.", "Transient failures go through delayed retry queues, then the DLQ."],
  },
  "anomaly-detector": {
    short: "AI worker",
    responsibility: "Scores transfer.completed events with an Isolation Forest and stores the result, asynchronously.",
    decisions: ["Transfers never wait for inference.", "Deduplicated by event id like every consumer."],
  },
  jaeger: {
    short: "Trace store",
    responsibility: "Optional (Compose profile `observability`): stores OpenTelemetry spans from the API and workers.",
    decisions: ["Tracing is observation only: when Jaeger is down, spans are dropped and requests are unaffected."],
  },
};

export function infoFor(id: string, kind: string): ComponentInfo {
  return COMPONENT_INFO[id] ?? (kind === "api" ? COMPONENT_INFO.api! : { short: kind, responsibility: "", decisions: [] });
}
