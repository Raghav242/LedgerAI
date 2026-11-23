import { useQuery } from "@tanstack/react-query";
import { Check, CircleHelp, Clock, Minus, Sparkles, X } from "lucide-react";
import { useState } from "react";
import { observabilityApi } from "@/api/observability";
import { queryKeys } from "@/api/queryKeys";
import type { TraceView } from "@/types/api";
import type { ExplorerStage, StageEvidence } from "./lifecycleModel";

const POLL_MS = 1_500;
const MAX_POLL_MS = 120_000;

export const EVIDENCE_STYLE: Record<StageEvidence, { label: string; icon: typeof Check; text: string; ring: string }> = {
  observed: { label: "Observed", icon: Check, text: "text-success", ring: "border-success/50 bg-success/10" },
  derived: { label: "Derived", icon: Sparkles, text: "text-info", ring: "border-info/40 bg-info/5" },
  pending: { label: "Pending", icon: Clock, text: "text-muted-foreground", ring: "border-dashed" },
  failed: { label: "Failed (will retry)", icon: X, text: "text-destructive", ring: "border-destructive/50 bg-destructive/10" },
  unknown: { label: "Unknown", icon: CircleHelp, text: "text-muted-foreground", ring: "border-dashed" },
  not_applicable: { label: "Not applicable", icon: Minus, text: "text-muted-foreground", ring: "border-dashed opacity-60" },
};

export function busy(stages: ExplorerStage[]): boolean {
  return stages.some((s) => s.evidence === "pending" || s.evidence === "failed");
}

/** Polls the persisted lifecycle (and, if a trace id is known, the trace) until nothing is pending. */
export function useTransferLifecycle(transferId: string | null, traceId: string | null | undefined) {
  const [startedAt] = useState(() => Date.now());
  const lifecycle = useQuery({
    queryKey: queryKeys.observability.transferLifecycle(transferId ?? ""),
    queryFn: ({ signal }) => observabilityApi.transferLifecycle(transferId!, signal),
    enabled: !!transferId,
    refetchInterval: (q) => {
      const data = q.state.data;
      if (q.state.error || Date.now() - startedAt > MAX_POLL_MS) return false;
      const waiting = !data || data.stages.some((s) => s.state === "pending" || s.state === "failed");
      return waiting ? POLL_MS : false;
    },
  });
  // The response header names the trace in the Playground; otherwise the outbox row does.
  const effectiveTraceId = traceId ?? lifecycle.data?.event?.trace_id ?? null;
  const trace = useQuery({
    queryKey: queryKeys.observability.trace(effectiveTraceId ?? ""),
    queryFn: ({ signal }) => observabilityApi.trace(effectiveTraceId!, signal),
    enabled: !!effectiveTraceId,
    // Spans arrive with the exporter's small delay, the async ones later still.
    refetchInterval: (q) => {
      const data: TraceView | undefined = q.state.data;
      if (q.state.error || q.state.dataUpdateCount > 20) return false;
      if (!data || !data.available) return 2_000;
      const consumed = lifecycle.data?.stages.filter((s) => s.id.startsWith("consume:") && s.state === "observed").length ?? 0;
      const consumerSpans = data.spans.filter((s) => s.name.startsWith("event.consume")).length;
      return consumerSpans < consumed ? 2_000 : false;
    },
  });
  return { lifecycle, trace, traceId: effectiveTraceId };
}

