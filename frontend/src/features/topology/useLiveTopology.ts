import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { observabilityApi } from "@/api/observability";
import { queryKeys } from "@/api/queryKeys";
import type { ComponentHealth, TopologySnapshot } from "@/types/api";

export const TOPOLOGY_POLL_MS = 5_000;
/** A snapshot older than this is not shown as current (polls failed or stopped). */
export const SNAPSHOT_STALE_MS = 15_000;

export function useNow(intervalMs = 1_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

export interface LiveTopology {
  snapshot: TopologySnapshot | undefined;
  /** Components as they should be DISPLAYED: when the snapshot is stale, statuses become "stale". */
  components: ComponentHealth[];
  isLoading: boolean;
  error: unknown;
  /** Milliseconds since the last successful snapshot, or null before the first one. */
  snapshotAgeMs: number | null;
  stale: boolean;
  refetch: () => void;
}

/** The browser itself: observed by definition (this code is running). */
function browserComponent(): ComponentHealth {
  const now = new Date().toISOString();
  return {
    id: "browser",
    name: "Browser",
    kind: "gateway",
    status: "healthy",
    evidence: "request",
    source: "this page",
    message: "This page is running and polling the backend.",
    observed_at: now,
    age_seconds: 0,
    latency_ms: null,
    last_ok_at: now,
    last_error: null,
    last_error_at: null,
    metrics: [],
  };
}

export function useLiveTopology(): LiveTopology {
  const query = useQuery({
    queryKey: queryKeys.observability.topology,
    queryFn: ({ signal }) => observabilityApi.topology(signal),
    refetchInterval: TOPOLOGY_POLL_MS,
    refetchIntervalInBackground: false,
    retry: false,
  });
  const now = useNow();
  const snapshotAgeMs = query.data ? Math.max(0, now - query.dataUpdatedAt) : null;
  const stale = snapshotAgeMs !== null && snapshotAgeMs > SNAPSHOT_STALE_MS;
  const fromServer = (query.data?.components ?? []).map((c) =>
    stale && c.status !== "not_configured"
      ? { ...c, status: "stale" as const, message: `Last known: ${c.status}. ${c.message}` }
      : c,
  );
  return {
    snapshot: query.data,
    components: query.data ? [browserComponent(), ...fromServer] : [],
    isLoading: query.isLoading,
    error: query.error,
    snapshotAgeMs,
    stale,
    refetch: () => void query.refetch(),
  };
}
