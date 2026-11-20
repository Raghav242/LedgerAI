/**
 * Experiment Lab fixtures. CATALOG is a verbatim copy of GET /api/v1/lab/experiments from
 * the real backend; runs and events are built to the same schema (app/lab/schemas.py).
 */
import type { ExperimentDefinition, LabEvent, LabRequestObservation, LabRun } from "@/types/api";

export const CATALOG: ExperimentDefinition[] = [
  {
    "id": "concurrency",
    "title": "Concurrent withdrawals",
    "summary": "Many transfers race to spend the same balance at the same instant.",
    "concept": "Row locking (SELECT … FOR UPDATE) and atomic transactions",
    "expected": [
      "Exactly funding ÷ amount transfers succeed (10 with the defaults).",
      "Every other request is rejected with 409 INSUFFICIENT_FUNDS.",
      "The source balance never goes negative; money is conserved.",
      "Ledger and reconciliation invariants still hold."
    ],
    "how_it_works": [
      "Creates two isolated lab accounts and funds the source.",
      "Releases all transfers at once through NGINX to every replica.",
      "Retries only retryable answers, always with the same Idempotency-Key.",
      "Checks balances, the ledger and global reconciliation."
    ],
    "risk": "safe_local",
    "risk_note": "Local development only. Creates its own uniquely named lab accounts funded with simulated money and never touches other accounts. Bounded request counts, one run at a time, no infrastructure control.",
    "available": true,
    "unavailable_reason": null,
    "estimated_seconds": 5,
    "params": [
      {
        "name": "requests",
        "label": "Concurrent requests",
        "description": "Transfers released at the same instant.",
        "unit": "requests",
        "default": 100,
        "minimum": 2,
        "maximum": 200
      },
      {
        "name": "amount_minor",
        "label": "Amount per transfer",
        "description": "Each withdrawal, in cents.",
        "unit": "cents",
        "default": 1000,
        "minimum": 1,
        "maximum": 100000
      },
      {
        "name": "funding_minor",
        "label": "Starting balance",
        "description": "Simulated money minted into the isolated source account, in cents.",
        "unit": "cents",
        "default": 10000,
        "minimum": 1,
        "maximum": 1000000
      }
    ]
  },
  {
    "id": "idempotency",
    "title": "Idempotent retries",
    "summary": "One transfer sent many times concurrently with the same Idempotency-Key.",
    "concept": "Idempotency keys: claim-and-complete in one transaction, replay on retry",
    "expected": [
      "Exactly one request executes the transfer; all others are replays.",
      "Every response describes the same transfer, byte for byte.",
      "Money moves once. Reusing the key with a different amount is rejected (422).",
      "A new key is a new, separate transfer."
    ],
    "how_it_works": [
      "Creates two isolated lab accounts and funds the source.",
      "Fires every duplicate at once with one key, through NGINX.",
      "Probes key reuse with a different body, then a fresh key.",
      "Counts committed transfers in the database and reconciles the ledger."
    ],
    "risk": "safe_local",
    "risk_note": "Local development only. Creates its own uniquely named lab accounts funded with simulated money and never touches other accounts. Bounded request counts, one run at a time, no infrastructure control.",
    "available": true,
    "unavailable_reason": null,
    "estimated_seconds": 4,
    "params": [
      {
        "name": "duplicates",
        "label": "Duplicate requests",
        "description": "Concurrent copies of ONE transfer, all with the same Idempotency-Key.",
        "unit": "requests",
        "default": 20,
        "minimum": 2,
        "maximum": 100
      },
      {
        "name": "amount_minor",
        "label": "Transfer amount",
        "description": "The single logical transfer, in cents.",
        "unit": "cents",
        "default": 1000,
        "minimum": 1,
        "maximum": 100000
      }
    ]
  },
  {
    "id": "rate_limiting",
    "title": "Distributed rate limiting",
    "summary": "A burst of writes against the real write quota, shared by every replica.",
    "concept": "A global token bucket in Redis, enforced atomically across replicas",
    "expected": [
      "About `capacity` requests are admitted (plus tokens refilled during the burst).",
      "The rest get 429 RATE_LIMITED with a Retry-After header and move no money.",
      "Admitted requests are spread over replicas, yet the quota is global.",
      "After the wait, roughly wait x refill-rate tokens are available again."
    ],
    "how_it_works": [
      "Uses the API's real write policy on a fresh bucket that belongs to this run only, so your own browser quota is untouched.",
      "Fires the burst through NGINX, waits, then fires a second burst.",
      "Compares with what independent per-replica counters would have admitted."
    ],
    "risk": "safe_local",
    "risk_note": "Local development only. Creates its own uniquely named lab accounts funded with simulated money and never touches other accounts. Bounded request counts, one run at a time, no infrastructure control.",
    "available": true,
    "unavailable_reason": null,
    "estimated_seconds": 6,
    "params": [
      {
        "name": "burst_requests",
        "label": "Burst size",
        "description": "Concurrent write requests fired at once against a fresh bucket.",
        "unit": "requests",
        "default": 50,
        "minimum": 5,
        "maximum": 150
      },
      {
        "name": "refill_wait_seconds",
        "label": "Refill wait",
        "description": "Pause after the burst so the bucket can refill.",
        "unit": "seconds",
        "default": 2,
        "minimum": 0,
        "maximum": 5
      },
      {
        "name": "probe_requests",
        "label": "Requests after the wait",
        "description": "Second burst that measures how many tokens came back (0 = skip).",
        "unit": "requests",
        "default": 30,
        "minimum": 0,
        "maximum": 60
      }
    ]
  },
  {
    "id": "load_balancing",
    "title": "Load balancing",
    "summary": "Read requests through NGINX, recording which replica served each one.",
    "concept": "Round-robin load balancing over stateless replicas",
    "expected": [
      "Requests are spread evenly across the running replicas.",
      "Every request succeeds, whichever replica serves it.",
      "The sequence shows the round-robin pattern (1, 2, 1, 2, …)."
    ],
    "how_it_works": [
      "Creates one isolated lab account to read.",
      "Sends the requests through NGINX and records X-Served-By per response.",
      "Summarises the distribution and its spread. No replica is stopped."
    ],
    "risk": "safe_local",
    "risk_note": "Local development only. Creates its own uniquely named lab accounts funded with simulated money and never touches other accounts. Bounded request counts, one run at a time, no infrastructure control.",
    "available": true,
    "unavailable_reason": null,
    "estimated_seconds": 3,
    "params": [
      {
        "name": "requests",
        "label": "Requests",
        "description": "Read requests sent through NGINX.",
        "unit": "requests",
        "default": 40,
        "minimum": 2,
        "maximum": 200
      },
      {
        "name": "concurrency",
        "label": "In flight at once",
        "description": "1 sends them one after another, which shows the round-robin pattern most clearly.",
        "unit": "requests",
        "default": 1,
        "minimum": 1,
        "maximum": 20
      }
    ]
  }
];

export const RUN_ID = "aaaaaaaa-1111-4aaa-8aaa-aaaaaaaaaaaa";

export function labRun(overrides: Partial<LabRun> = {}): LabRun {
  return {
    id: RUN_ID,
    experiment: "concurrency",
    status: "running",
    params: { requests: 100, amount_minor: 1000, funding_minor: 10000 },
    progress: { phase: "burst", message: "Releasing 100 concurrent $10.00 transfers", completed: 40, total: 100 },
    result: null,
    checks: null,
    error: null,
    runner_replica: "api-1",
    target_base_url: "http://nginx:8080",
    event_count: 0,
    events_dropped: 0,
    created_at: "2026-11-18T12:00:00Z",
    started_at: "2026-11-18T12:00:00Z",
    finished_at: null,
    heartbeat_at: "2026-11-18T12:00:01Z",
    ...overrides,
  };
}

export function observation(overrides: Partial<LabRequestObservation> = {}): LabRequestObservation {
  return {
    index: 0,
    attempt: 1,
    lane: "withdrawal",
    method: "POST",
    path: "/api/v1/transfers",
    status: 201,
    code: null,
    outcome: "committed",
    replica: "api-1",
    latency_ms: 12.5,
    replayed: false,
    retry_after_s: null,
    rate_policy: "lab-burst-write",
    rate_remaining: 999,
    request_id: "req-1",
    ...overrides,
  };
}

export function requestEvent(seq: number, request: LabRequestObservation): LabEvent {
  return {
    seq,
    t_ms: seq * 10,
    kind: "request",
    level: request.outcome === "committed" || request.outcome === "read_ok" ? "success" : "warning",
    title: `#${request.index + 1} ${request.method} → ${request.status ?? "no response"}${request.code ? ` ${request.code}` : ""}`,
    detail: null,
    phase: "burst",
    request,
  };
}

/** A finished concurrency run: 10 committed, 90 insufficient funds over two replicas. */
export function passedConcurrencyRun(): LabRun {
  return labRun({
    status: "passed",
    progress: { phase: "done", message: "Finished", completed: 100, total: 100 },
    finished_at: "2026-11-18T12:00:03Z",
    event_count: 4,
    result: {
      summary: "100 concurrent withdrawals of $10.00 from $100.00: 10 succeeded, 90 were rejected for insufficient funds, and the source ended at $0.00.",
      explanation: ["Every transfer locks both account rows.", "Only 10 can fit."],
      metrics: [
        { key: "total", label: "Total requests", value: 100, tone: "neutral", unit: null, hint: null },
        { key: "succeeded", label: "Succeeded", value: 10, tone: "success", unit: null, hint: null },
        { key: "rejected", label: "Insufficient funds", value: 90, tone: "warning", unit: null, hint: "409, final answer" },
      ],
      outcome_counts: { committed: 10, insufficient_funds: 90 },
      data: {
        expected_successes: 10,
        final_balances: { source: 0, destination: 10000 },
        by_replica: { "api-1": { committed: 5, insufficient_funds: 45 }, "api-2": { committed: 5, insufficient_funds: 45 } },
      },
    },
    checks: [
      { id: "exact_successes", label: "Exactly 10 transfers succeeded", status: "pass", observed: "10 succeeded", description: null },
      { id: "no_overdraft", label: "Source balance never negative", status: "pass", observed: "final source balance $0.00", description: null },
      { id: "reconciliation", label: "Global reconciliation passes", status: "pass", observed: "ledger total 0", description: null },
    ],
  });
}
