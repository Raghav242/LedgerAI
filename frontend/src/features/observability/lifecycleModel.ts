/**
 * Request lifecycle explorer model: the eight stages of one money movement, each with the
 * evidence it rests on. Pure functions, no React.
 *
 * Three independent sources, never mixed up:
 *  - the HTTP response the browser itself received (status, headers, measured round trip)
 *  - PERSISTED state read from the backend (transfer row, outbox row, consumer receipts)
 *  - optional OpenTelemetry spans (only when backend tracing is on and the trace was stored)
 *
 * A timing is shown only where one of these measured it. A stage nothing reports on is
 * "unknown" (or "derived" when it follows necessarily from another observation, e.g. a
 * committed transfer must have been admitted by the rate limiter).
 */
import type { ResponseMeta } from "@/api/client";
import type { EventLifecycle, LifecycleStage, TraceView } from "@/types/api";

export type StageEvidence = "observed" | "derived" | "pending" | "failed" | "unknown" | "not_applicable";

export interface ExplorerStage {
  id: string;
  label: string;
  evidence: StageEvidence;
  detail: string;
  /** Where the evidence came from, e.g. "response header", "trace span", "outbox row". */
  source: string | null;
  durationMs: number | null;
  async: boolean;
}

export interface SyncFacts {
  /** What the browser received for this transfer (Transaction Playground); null for history items. */
  meta: ResponseMeta | null;
  status: number | null;
  replayed: boolean;
}

type Span = TraceView["spans"][number];

function span(trace: TraceView | null | undefined, name: string): Span | undefined {
  return trace?.available ? trace.spans.find((s) => s.name === name) : undefined;
}

function serverSpan(trace: TraceView | null | undefined): Span | undefined {
  return trace?.available ? trace.spans.find((s) => s.parent_id === null && s.service === "ledgerflow-api") : undefined;
}

function fromPersisted(stage: LifecycleStage | undefined, fallback: Omit<ExplorerStage, "async">, isAsync: boolean): ExplorerStage {
  if (!stage) return { ...fallback, async: isAsync };
  const evidence: StageEvidence = stage.state === "observed" ? "observed" : stage.state === "failed" ? "failed" : stage.state;
  return {
    id: fallback.id,
    label: fallback.label,
    evidence,
    detail: stage.detail,
    source: stage.state === "observed" || stage.state === "failed" ? "persisted state" : null,
    durationMs: stage.duration_ms ?? null,
    async: isAsync,
  };
}

export function buildLifecycle({
  sync,
  lifecycle,
  trace,
}: {
  sync: SyncFacts | null;
  lifecycle: EventLifecycle | null | undefined;
  trace?: TraceView | null;
}): ExplorerStage[] {
  const meta = sync?.meta ?? null;
  const committed = !!lifecycle?.stages.find((s) => s.id === "commit" && s.state === "observed");
  const root = serverSpan(trace);
  const rate = span(trace, "rate_limit.check");
  const claim = span(trace, "idempotency.claim");
  const post = span(trace, "ledger.post");
  const create = span(trace, "transfer.create");
  const stages: ExplorerStage[] = [];

  // 1. Request received
  stages.push(
    root
      ? { id: "received", label: "Request received", evidence: "observed", detail: `${root.name} on ${root.instance ?? root.service}.`, source: "trace span", durationMs: root.duration_ms, async: false }
      : meta?.requestId
        ? { id: "received", label: "Request received", evidence: "observed", detail: `NGINX stamped X-Request-ID ${meta.requestId.slice(0, 12)}…`, source: "response header", durationMs: null, async: false }
        : committed
          ? { id: "received", label: "Request received", evidence: "derived", detail: "A committed transfer implies the request reached the API.", source: null, durationMs: null, async: false }
          : { id: "received", label: "Request received", evidence: "unknown", detail: "Not recorded for this item.", source: null, durationMs: null, async: false },
  );

  // 2. Replica selected
  const replica = meta?.servedBy ?? root?.instance ?? null;
  stages.push(
    replica
      ? { id: "replica", label: "Replica selected", evidence: "observed", detail: `NGINX routed it to ${replica}.`, source: meta?.servedBy ? "X-Served-By header" : "trace span", durationMs: null, async: false }
      : { id: "replica", label: "Replica selected", evidence: "unknown", detail: "Which replica served it is not stored with the transfer.", source: null, durationMs: null, async: false },
  );

  // 3. Rate limit
  if (rate) {
    stages.push({ id: "rate_limit", label: "Rate-limit decision", evidence: "observed", detail: `Token bucket: ${String(rate.attributes["ledgerflow.rate_limit.decision"] ?? "checked")}.`, source: "trace span", durationMs: rate.duration_ms, async: false });
  } else if (meta?.rateLimit.policy) {
    stages.push({ id: "rate_limit", label: "Rate-limit decision", evidence: "observed", detail: `Admitted by the ${meta.rateLimit.policy} bucket; ${meta.rateLimit.remaining ?? "?"} of ${meta.rateLimit.limit ?? "?"} tokens left.`, source: "X-RateLimit headers", durationMs: null, async: false });
  } else if (committed) {
    stages.push({ id: "rate_limit", label: "Rate-limit decision", evidence: "derived", detail: "Admitted: a rejected request never reaches the database.", source: null, durationMs: null, async: false });
  } else {
    stages.push({ id: "rate_limit", label: "Rate-limit decision", evidence: "unknown", detail: "Not recorded.", source: null, durationMs: null, async: false });
  }

  // 4. Idempotency
  if (sync?.replayed) {
    stages.push({ id: "idempotency", label: "Idempotency evaluation", evidence: "observed", detail: "Key already completed: the stored response was replayed; nothing executed again.", source: "Idempotent-Replayed header", durationMs: claim?.duration_ms ?? null, async: false });
  } else if (claim) {
    stages.push({ id: "idempotency", label: "Idempotency evaluation", evidence: "observed", detail: claim.attributes["ledgerflow.idempotency.new_claim"] === false ? "Key found: replay." : "New key claimed inside the transaction.", source: "trace span", durationMs: claim.duration_ms, async: false });
  } else if (committed) {
    stages.push({ id: "idempotency", label: "Idempotency evaluation", evidence: "derived", detail: "Every transfer claims its key in the same transaction (the outcome is stored with it).", source: null, durationMs: null, async: false });
  } else {
    stages.push({ id: "idempotency", label: "Idempotency evaluation", evidence: "unknown", detail: "Not recorded.", source: null, durationMs: null, async: false });
  }

  // 5. Database transaction
  const commit = lifecycle?.stages.find((s) => s.id === "commit");
  stages.push(
    commit
      ? {
          id: "database",
          label: "Database transaction",
          evidence: "observed",
          detail: sync?.replayed ? "The original transaction (replays execute nothing)." : "Rows locked, balances + ledger + outbox row committed together.",
          source: post || create ? "transfer row + trace span" : "transfer row",
          durationMs: create?.duration_ms ?? post?.duration_ms ?? null,
          async: false,
        }
      : { id: "database", label: "Database transaction", evidence: lifecycle === undefined ? "pending" : "unknown", detail: "Waiting for the persisted record.", source: null, durationMs: null, async: false },
  );

  // 6. Response
  stages.push(
    sync?.status
      ? { id: "response", label: "Transaction response", evidence: "observed", detail: `HTTP ${sync.status}${sync.replayed ? " (replayed)" : ""}.`, source: "browser", durationMs: meta?.durationMs ?? null, async: false }
      : committed
        ? { id: "response", label: "Transaction response", evidence: "derived", detail: "A 201 was sent when the transaction committed; this page did not receive it.", source: null, durationMs: null, async: false }
        : { id: "response", label: "Transaction response", evidence: "unknown", detail: "Not recorded.", source: null, durationMs: null, async: false },
  );

  // 7. Publication (async)
  const publish = lifecycle?.stages.find((s) => s.id === "publish");
  const publishSpan = span(trace, "outbox.publish");
  const pub = fromPersisted(publish, { id: "publish", label: "Event published (after COMMIT)", evidence: lifecycle ? "not_applicable" : "pending", detail: lifecycle ? "No event for this movement." : "Loading persisted state…", source: null, durationMs: null }, true);
  if (publishSpan && pub.evidence === "observed") pub.source = "outbox row + trace span";
  stages.push(pub);

  // 8. Consumers (async)
  const consumers = lifecycle?.stages.filter((s) => s.id.startsWith("consume:")) ?? [];
  if (consumers.length === 0) {
    stages.push({ id: "consume", label: "Event consumer processing", evidence: lifecycle ? "not_applicable" : "pending", detail: lifecycle ? "No consumer applies to this movement." : "Loading persisted state…", source: null, durationMs: null, async: true });
  } else {
    for (const c of consumers) {
      stages.push(fromPersisted(c, { id: c.id, label: c.label, evidence: "pending", detail: c.detail, source: null, durationMs: null }, true));
    }
  }
  return stages;
}

/** Done polling: nothing is still expected to change. */
export function settled(stages: ExplorerStage[]): boolean {
  return stages.every((s) => s.evidence !== "pending");
}
