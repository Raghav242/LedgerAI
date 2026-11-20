/**
 * Request Flow model: which components a request touched and what each one decided.
 *
 * Pure functions, no React. The path is DERIVED from facts the system actually reports
 * (HTTP status, error code, X-Served-By, Idempotent-Replayed, rate-limit headers) and from
 * the server's documented order of operations (app/api/routes/transfers.py,
 * app/services/transfer_service.py). It is not distributed tracing: nothing here claims a
 * component was observed directly unless the response says so.
 */

export type FlowState = "idle" | "pending" | "active" | "success" | "blocked" | "failed" | "skipped" | "replayed";

export type FlowNodeId = "client" | "nginx" | "replica" | "redis" | "idempotency" | "postgres" | "ledger" | "response";
export type AsyncNodeId = "outbox" | "rabbitmq" | "worker";

/** Outcomes reported by the lab backend (RequestOutcome) plus two the playground can see. */
export type FlowOutcome =
  | "committed"
  | "replayed"
  | "insufficient_funds"
  | "rate_limited"
  | "in_progress"
  | "conflict"
  | "key_reused"
  | "read_ok"
  | "rejected"
  | "invalid"
  | "limiter_unavailable"
  | "server_error"
  | "transport_error";

export interface FlowNodeStat {
  label: string;
  value: number;
  tone: "success" | "warning" | "destructive" | "info" | "muted";
}

export interface FlowNode {
  state: FlowState;
  /** What this component did for this request, in a few words. */
  detail?: string;
  /** Aggregate mode: how many requests ended in which state at this node. */
  stats?: FlowNodeStat[];
}

/**
 * The six components drawn by default. "database" groups the three PostgreSQL stages
 * (idempotency claim, row locks + balances, ledger postings), shown as sub-steps in its details.
 */
export type DisplayNodeId = "client" | "nginx" | "replica" | "redis" | "database" | "response";
export const DISPLAY_ORDER: DisplayNodeId[] = ["client", "nginx", "replica", "redis", "database", "response"];
export const DISPLAY_MEMBERS: Record<DisplayNodeId, FlowNodeId[]> = {
  client: ["client"],
  nginx: ["nginx"],
  replica: ["replica"],
  redis: ["redis"],
  database: ["idempotency", "postgres", "ledger"],
  response: ["response"],
};

/**
 * How we know what a component did:
 *  observed        reported directly by the response (status, error code, headers, body)
 *  inferred        deduced from the response plus the server's documented order of operations
 *  pending         expected to happen later (after commit); not observed by this request
 *  unknown         no evidence either way (e.g. no response arrived)
 *  not_applicable  the request never reached this component
 *  illustration    static teaching diagram (System Topology), not a real request
 */
export type Evidence = "observed" | "inferred" | "pending" | "unknown" | "not_applicable" | "illustration";

export interface FlowModel {
  nodes: Record<FlowNodeId, FlowNode>;
  /** The six drawn components (database = grouped PostgreSQL stages). */
  display: Record<DisplayNodeId, FlowNode>;
  evidence: Record<DisplayNodeId | AsyncNodeId, Evidence>;
  async: Record<AsyncNodeId, FlowNode>;
  /** Replicas to draw behind NGINX, and how each was involved. */
  replicas: { name: string; state: FlowState; count?: number }[];
  /** One sentence describing the path. */
  caption: string;
  /** HTTP status shown on the response node, when known. */
  status?: number | null;
}

export const FLOW_ORDER: FlowNodeId[] = ["client", "nginx", "replica", "redis", "idempotency", "postgres", "ledger", "response"];
export const ASYNC_ORDER: AsyncNodeId[] = ["outbox", "rabbitmq", "worker"];

export const NODE_META: Record<FlowNodeId | AsyncNodeId | "database", { label: string; role: string }> = {
  client: { label: "Client", role: "Browser or lab runner" },
  nginx: { label: "NGINX", role: "Load balancer" },
  replica: { label: "API replica", role: "FastAPI" },
  redis: { label: "Redis", role: "Rate limiter" },
  idempotency: { label: "Idempotency", role: "Key claim (PostgreSQL)" },
  postgres: { label: "PostgreSQL", role: "Row locks + balances" },
  ledger: { label: "Ledger", role: "Double-entry postings" },
  response: { label: "Response", role: "HTTP answer" },
  database: { label: "PostgreSQL", role: "Key claim, row locks, ledger" },
  outbox: { label: "Outbox", role: "Event row, same commit" },
  rabbitmq: { label: "RabbitMQ", role: "Durable queue" },
  worker: { label: "Consumer", role: "Idempotent audit worker" },
};

type Path = Partial<Record<FlowNodeId, [FlowState, string?]>>;

const SKIP: [FlowState, string] = ["skipped", "not reached"];

/** Path for each outcome; every node not listed is `skipped`. */
const PATHS: Record<FlowOutcome, { path: Path; caption: string; asyncState: FlowState }> = {
  committed: {
    path: {
      redis: ["success", "token taken"],
      idempotency: ["success", "key claimed"],
      postgres: ["success", "rows locked, balances updated"],
      ledger: ["success", "debit + credit posted"],
      response: ["success", "201 Created"],
    },
    caption: "Admitted by the rate limiter, key claimed, both rows locked and updated, ledger posted, one COMMIT.",
    asyncState: "pending",
  },
  replayed: {
    path: {
      redis: ["success", "token taken"],
      idempotency: ["replayed", "stored outcome found"],
      postgres: SKIP,
      ledger: SKIP,
      response: ["replayed", "Idempotent-Replayed"],
    },
    caption: "The key already had a stored outcome, so the original response was replayed. No balance was touched.",
    asyncState: "skipped",
  },
  insufficient_funds: {
    path: {
      redis: ["success", "token taken"],
      idempotency: ["success", "key claimed, rejection stored"],
      postgres: ["blocked", "balance too low after locking"],
      ledger: SKIP,
      response: ["blocked", "409 INSUFFICIENT_FUNDS"],
    },
    caption: "Rows were locked and the current balance checked: not enough money. Rolled back to the savepoint; the rejection is the key's final answer.",
    asyncState: "skipped",
  },
  rate_limited: {
    path: {
      redis: ["blocked", "bucket empty"],
      response: ["blocked", "429 + Retry-After"],
    },
    caption: "The shared token bucket in Redis was empty. Rejected before any database work; safe to retry after Retry-After.",
    asyncState: "skipped",
  },
  in_progress: {
    path: {
      redis: ["success", "token taken"],
      idempotency: ["blocked", "same key still running"],
      response: ["blocked", "409 IN_PROGRESS"],
    },
    caption: "Another request with the same key was still running. Nothing executed; retry with the same key.",
    asyncState: "skipped",
  },
  conflict: {
    path: {
      redis: ["success", "token taken"],
      idempotency: ["success", "key claimed"],
      postgres: ["failed", "lock wait timed out"],
      ledger: SKIP,
      response: ["failed", "503 CONCURRENCY_CONFLICT"],
    },
    caption: "Waited too long for a row lock and rolled back everything, including the key claim. Retry with the same key.",
    asyncState: "skipped",
  },
  key_reused: {
    path: {
      redis: ["success", "token taken"],
      idempotency: ["blocked", "different body for this key"],
      response: ["blocked", "422 KEY_REUSED"],
    },
    caption: "The key was already bound to a different request body. Refused without moving money.",
    asyncState: "skipped",
  },
  read_ok: {
    path: {
      redis: ["success", "read token taken"],
      idempotency: SKIP,
      postgres: ["success", "row read"],
      ledger: SKIP,
      response: ["success", "200 OK"],
    },
    caption: "A read: admitted by the read quota and answered from PostgreSQL. Reads need no idempotency key.",
    asyncState: "skipped",
  },
  rejected: {
    path: {
      redis: ["success", "token taken"],
      idempotency: ["success", "key claimed"],
      postgres: ["blocked", "business rule"],
      ledger: SKIP,
      response: ["blocked", "rejected"],
    },
    caption: "A business rule rejected the request after the key was claimed. Nothing moved.",
    asyncState: "skipped",
  },
  invalid: {
    path: {
      replica: ["blocked", "validation failed"],
      redis: SKIP,
      response: ["blocked", "422 VALIDATION_ERROR"],
    },
    caption: "The request body failed validation in the replica. Nothing was executed.",
    asyncState: "skipped",
  },
  limiter_unavailable: {
    path: {
      redis: ["failed", "unreachable: fail closed"],
      response: ["failed", "503 LIMITER_UNAVAILABLE"],
    },
    caption: "Redis could not be reached, so the write failed closed. Nothing executed; retry with the same key.",
    asyncState: "skipped",
  },
  server_error: {
    path: {
      replica: ["failed", "server error"],
      redis: ["idle", "unknown"],
      idempotency: ["idle", "unknown"],
      postgres: ["idle", "unknown"],
      ledger: ["idle", "unknown"],
      response: ["failed", "5xx"],
    },
    caption: "The server failed. The outcome may be unknown: only a retry with the same key can tell, and it can never double-charge.",
    asyncState: "idle",
  },
  transport_error: {
    path: {
      nginx: ["failed", "no response"],
      replica: ["idle", "unknown"],
      redis: ["idle", "unknown"],
      idempotency: ["idle", "unknown"],
      postgres: ["idle", "unknown"],
      ledger: ["idle", "unknown"],
      response: ["failed", "no response"],
    },
    caption: "No HTTP response arrived. Whether anything executed is unknown; retry with the same key.",
    asyncState: "idle",
  },
};

export interface FlowOptions {
  /** The replica that served the request (X-Served-By), if known. */
  replica?: string | null;
  /** Replicas to draw. Defaults to the serving replica only. */
  replicas?: string[];
  status?: number | null;
  /** Node detail overrides, e.g. remaining tokens for Redis. */
  details?: Partial<Record<FlowNodeId, string>>;
  /** The response carried X-Request-ID (NGINX stamps it). */
  requestId?: boolean;
  /** The response carried X-RateLimit-* headers (the limiter answered). */
  rateHeaders?: boolean;
}

function nodeEntry([state, detail]: [FlowState, string?]): FlowNode {
  return { state, detail };
}

/** Precedence when several states are present: the most informative one colours the node. */
const PRECEDENCE: FlowState[] = ["failed", "blocked", "replayed", "success", "pending", "active", "idle", "skipped"];

/** State of a grouped component: skipped only if every stage was skipped, else the most informative stage. */
export function groupState(states: FlowState[]): FlowState {
  const reached = states.filter((s) => s !== "skipped");
  if (reached.length === 0) return "skipped";
  return [...reached].sort((a, b) => PRECEDENCE.indexOf(a) - PRECEDENCE.indexOf(b))[0]!;
}

function displayOf(nodes: Record<FlowNodeId, FlowNode>): Record<DisplayNodeId, FlowNode> {
  const out = {} as Record<DisplayNodeId, FlowNode>;
  for (const id of DISPLAY_ORDER) {
    const members = DISPLAY_MEMBERS[id].map((m) => nodes[m]);
    const state = groupState(members.map((m) => m.state));
    out[id] = { state, detail: members.find((m) => m.state === state)?.detail };
  }
  return out;
}

/** Outcomes whose response body/headers directly prove what PostgreSQL did. */
const OBSERVED_DATABASE: FlowOutcome[] = ["committed", "replayed", "read_ok"];

function evidenceFor(state: FlowState, known: Evidence): Evidence {
  return state === "skipped" ? "not_applicable" : state === "idle" ? "unknown" : known;
}

function asyncEvidence(id: AsyncNodeId, state: FlowState): Evidence {
  if (state === "skipped") return "not_applicable";
  if (state === "idle") return "unknown";
  // The outbox row is written in the same COMMIT by design; publishing and consuming happen
  // later and are never observed by the request itself.
  return id === "outbox" ? "inferred" : "pending";
}

/** The path of ONE request. */
export function flowForOutcome(outcome: FlowOutcome, options: FlowOptions = {}): FlowModel {
  const def = PATHS[outcome];
  const reachedReplica = outcome !== "transport_error";
  const nodes = {} as Record<FlowNodeId, FlowNode>;
  for (const id of FLOW_ORDER) {
    const step = def.path[id];
    if (step) nodes[id] = nodeEntry(step);
    else if (id === "client") nodes[id] = { state: "success", detail: "request sent" };
    else if (id === "nginx") nodes[id] = { state: "success", detail: "forwarded (round robin)" };
    else if (id === "replica") nodes[id] = { state: "success", detail: options.replica ? `served by ${options.replica}` : "handled the request" };
    else nodes[id] = { state: "skipped", detail: "not reached" };
  }
  for (const [id, detail] of Object.entries(options.details ?? {}) as [FlowNodeId, string][]) {
    nodes[id] = { ...nodes[id], detail };
  }
  const names = options.replicas?.length ? options.replicas : options.replica ? [options.replica] : ["replica"];
  const servedBy = options.replica ?? (names.length === 1 ? names[0] : undefined);
  const pending = def.asyncState === "pending";
  const asyncNodes: Record<AsyncNodeId, FlowNode> = {
    outbox: { state: pending ? "success" : def.asyncState, detail: pending ? "event row written in the same COMMIT" : undefined },
    rabbitmq: { state: def.asyncState, detail: pending ? "published after commit (not observed here)" : undefined },
    worker: { state: def.asyncState, detail: pending ? "applied once by event id (not observed here)" : undefined },
  };
  const display = displayOf(nodes);

  const evidence = {} as FlowModel["evidence"];
  for (const id of DISPLAY_ORDER) {
    let known: Evidence = "inferred";
    if (id === "client" || id === "response") known = "observed";
    else if (id === "nginx") known = options.requestId || outcome === "transport_error" ? "observed" : "inferred";
    else if (id === "replica") known = options.replica ? "observed" : "inferred";
    else if (id === "redis")
      known = options.rateHeaders || outcome === "rate_limited" || outcome === "limiter_unavailable" ? "observed" : "inferred";
    else if (id === "database") known = OBSERVED_DATABASE.includes(outcome) ? "observed" : "inferred";
    evidence[id] = evidenceFor(display[id].state, known);
  }
  for (const id of ASYNC_ORDER) evidence[id] = asyncEvidence(id, asyncNodes[id].state);

  return {
    nodes,
    display,
    evidence,
    async: asyncNodes,
    replicas: names.map((name) => ({
      name,
      state: !reachedReplica ? "idle" : name === servedBy ? nodes.replica.state : "idle",
    })),
    caption: def.caption,
    status: options.status,
  };
}

// --- aggregate (many requests) -----------------------------------------------------------------

export interface ObservedRequest {
  outcome: FlowOutcome;
  replica: string | null;
  /** The response carried X-Request-ID. */
  requestId?: boolean;
  /** The response carried X-RateLimit-* headers. */
  rateHeaders?: boolean;
}

const TONE: Record<FlowState, FlowNodeStat["tone"]> = {
  success: "success",
  replayed: "info",
  blocked: "warning",
  failed: "destructive",
  active: "info",
  pending: "muted",
  idle: "muted",
  skipped: "muted",
};

const STAT_LABEL: Record<FlowState, string> = {
  success: "passed",
  replayed: "replayed",
  blocked: "blocked",
  failed: "failed",
  active: "active",
  pending: "pending",
  idle: "unknown",
  skipped: "skipped",
};

function summarize(states: FlowState[]): FlowNode {
  const counts = new Map<FlowState, number>();
  for (const s of states) counts.set(s, (counts.get(s) ?? 0) + 1);
  const relevant = [...counts.entries()].filter(([s]) => s !== "skipped");
  if (relevant.length === 0) return { state: "skipped", detail: "not reached" };
  const stats = [...relevant]
    .sort((a, b) => PRECEDENCE.indexOf(a[0]) - PRECEDENCE.indexOf(b[0]))
    .reverse()
    .map(([s, n]) => ({ label: STAT_LABEL[s], value: n, tone: TONE[s] }));
  // Colour by the majority among reached states; ties broken by precedence.
  const majority = [...relevant].sort((a, b) => b[1] - a[1] || PRECEDENCE.indexOf(a[0]) - PRECEDENCE.indexOf(b[0]))[0]![0];
  return { state: majority, stats };
}

/** The paths of MANY requests (an experiment run), folded into per-node counts. */
export function aggregateFlow(requests: ObservedRequest[], caption: string, replicaNames: string[] = []): FlowModel {
  const perNode = new Map<FlowNodeId, FlowState[]>(FLOW_ORDER.map((id) => [id, []]));
  const perDisplay = new Map<DisplayNodeId, FlowState[]>(DISPLAY_ORDER.map((id) => [id, []]));
  const perAsync = new Map<AsyncNodeId, FlowState[]>(ASYNC_ORDER.map((id) => [id, []]));
  const replicaCounts = new Map<string, number>();
  for (const r of requests) {
    const single = flowForOutcome(r.outcome, { replica: r.replica });
    for (const id of FLOW_ORDER) perNode.get(id)!.push(single.nodes[id].state);
    for (const id of DISPLAY_ORDER) perDisplay.get(id)!.push(single.display[id].state);
    for (const id of ASYNC_ORDER) perAsync.get(id)!.push(single.async[id].state);
    if (r.replica) replicaCounts.set(r.replica, (replicaCounts.get(r.replica) ?? 0) + 1);
  }
  const nodes = Object.fromEntries(FLOW_ORDER.map((id) => [id, summarize(perNode.get(id)!)])) as Record<FlowNodeId, FlowNode>;
  const display = Object.fromEntries(DISPLAY_ORDER.map((id) => [id, summarize(perDisplay.get(id)!)])) as Record<DisplayNodeId, FlowNode>;
  if (requests.length > 0) {
    const client: FlowNode = { state: "success", detail: `${requests.length} request${requests.length === 1 ? "" : "s"} sent` };
    nodes.client = client;
    display.client = client;
  }
  const asyncNodes = Object.fromEntries(ASYNC_ORDER.map((id) => [id, summarize(perAsync.get(id)!)])) as Record<AsyncNodeId, FlowNode>;
  if (asyncNodes.rabbitmq.state === "pending") asyncNodes.rabbitmq.detail = "published after commit (not observed here)";
  const names = [...new Set([...replicaNames, ...replicaCounts.keys()])].sort();

  const outcomes = new Set(requests.map((r) => r.outcome));
  const evidence = {} as FlowModel["evidence"];
  for (const id of DISPLAY_ORDER) {
    let known: Evidence = "inferred";
    if (id === "client" || id === "response") known = "observed";
    else if (id === "nginx") known = requests.some((r) => r.requestId) ? "observed" : "inferred";
    else if (id === "replica") known = replicaCounts.size > 0 ? "observed" : "inferred";
    else if (id === "redis") known = requests.some((r) => r.rateHeaders) || outcomes.has("rate_limited") ? "observed" : "inferred";
    else if (id === "database") known = OBSERVED_DATABASE.some((o) => outcomes.has(o)) ? "observed" : "inferred";
    evidence[id] = evidenceFor(display[id].state, known);
  }
  for (const id of ASYNC_ORDER) evidence[id] = asyncEvidence(id, asyncNodes[id].state);

  return {
    nodes,
    display,
    evidence,
    async: asyncNodes,
    replicas: (names.length ? names : ["replica"]).map((name) => ({
      name,
      state: replicaCounts.get(name) ? "success" : "idle",
      count: replicaCounts.get(name) ?? 0,
    })),
    caption,
  };
}

/** An all-idle model, for "nothing sent yet". */
export function idleFlow(replicas: string[] = ["api-1", "api-2"]): FlowModel {
  const nodes = Object.fromEntries(FLOW_ORDER.map((id) => [id, { state: "idle" as FlowState }])) as Record<FlowNodeId, FlowNode>;
  const display = Object.fromEntries(DISPLAY_ORDER.map((id) => [id, { state: "idle" as FlowState }])) as Record<DisplayNodeId, FlowNode>;
  const evidence = Object.fromEntries([...DISPLAY_ORDER, ...ASYNC_ORDER].map((id) => [id, "unknown" as Evidence])) as FlowModel["evidence"];
  return {
    nodes,
    display,
    evidence,
    async: { outbox: { state: "idle" }, rabbitmq: { state: "idle" }, worker: { state: "idle" } },
    replicas: replicas.map((name) => ({ name, state: "idle" })),
    caption: "Send a request to see the path it takes.",
  };
}

/** Marks a model as a static teaching diagram (System Topology): nothing in it was observed. */
export function asIllustration(model: FlowModel): FlowModel {
  const evidence = Object.fromEntries(Object.keys(model.evidence).map((id) => [id, "illustration" as Evidence])) as FlowModel["evidence"];
  return { ...model, evidence };
}

/** Maps a transfer error code (as the playground sees it) to a flow outcome. */
export function outcomeForCode(status: number | null, code: string, replayed: boolean): FlowOutcome {
  if (status === null) return "transport_error";
  if (status === 200 || status === 201) return replayed ? "replayed" : "committed";
  switch (code) {
    case "INSUFFICIENT_FUNDS":
      return "insufficient_funds";
    case "RATE_LIMITED":
      return "rate_limited";
    case "RATE_LIMITER_UNAVAILABLE":
      return "limiter_unavailable";
    case "IDEMPOTENCY_REQUEST_IN_PROGRESS":
      return "in_progress";
    case "CONCURRENCY_CONFLICT":
      return "conflict";
    case "IDEMPOTENCY_KEY_REUSED":
      return "key_reused";
    case "VALIDATION_ERROR":
      return "invalid";
  }
  if (status === 429) return "rate_limited";
  if (status >= 500) return "server_error";
  return "rejected";
}

export const OUTCOME_LABEL: Record<FlowOutcome, string> = {
  committed: "Committed (201)",
  replayed: "Replayed (201)",
  insufficient_funds: "Insufficient funds (409)",
  rate_limited: "Rate limited (429)",
  in_progress: "Key in progress (409)",
  conflict: "Lock conflict (503)",
  key_reused: "Key reused (422)",
  read_ok: "Read (200)",
  rejected: "Rejected (4xx)",
  invalid: "Invalid (422)",
  limiter_unavailable: "Limiter down (503)",
  server_error: "Server error (5xx)",
  transport_error: "No response",
};
