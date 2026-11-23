import type { TransferListParams } from "./transfers";

/**
 * Query key factory. Keys are hierarchical so one invalidation covers a whole family,
 * e.g. invalidating ["transfers"] refreshes every history page and filter combination.
 */
export const queryKeys = {
  accounts: {
    all: ["accounts"] as const,
    list: (limit: number, offset: number) => ["accounts", "list", { limit, offset, order: "desc" }] as const,
    detail: (id: string) => ["accounts", "detail", id] as const,
  },
  transfers: {
    all: ["transfers"] as const,
    list: (params: TransferListParams) => ["transfers", "list", params] as const,
    detail: (id: string) => ["transfers", "detail", id] as const,
    ledger: (id: string) => ["transfers", "ledger", id] as const,
  },
  ledger: {
    all: ["ledger"] as const,
    entries: (params: { account_id?: string; limit: number; offset: number }) => ["ledger", "entries", params] as const,
  },
  reconciliation: ["reconciliation"] as const,
  summary: (days: number) => ["summary", days] as const,
  summaryAll: ["summary"] as const,
  health: ["health", "ready"] as const,
  observability: {
    all: ["observability"] as const,
    topology: ["observability", "topology"] as const,
    pipeline: ["observability", "pipeline"] as const,
    events: (params: object) => ["observability", "events", params] as const,
    eventLifecycle: (id: string) => ["observability", "event", id] as const,
    transferLifecycle: (id: string) => ["observability", "transfer", id] as const,
    trace: (id: string) => ["observability", "trace", id] as const,
  },
  lab: {
    all: ["lab"] as const,
    experiments: ["lab", "experiments"] as const,
    runs: ["lab", "runs"] as const,
    run: (id: string) => ["lab", "run", id] as const,
    events: (id: string) => ["lab", "events", id] as const,
  },
};
