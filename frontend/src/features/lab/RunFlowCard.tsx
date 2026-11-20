import { useMemo, useState, type ReactNode } from "react";
import { aggregateFlow, flowForOutcome, OUTCOME_LABEL, type FlowOutcome } from "@/components/lab/flow/flowModel";
import { RequestFlowDiagram } from "@/components/lab/flow/RequestFlowDiagram";
import { cn } from "@/lib/utils";
import type { LabRequestObservation } from "@/types/api";

export interface RunFlowProps {
  requests: LabRequestObservation[];
  /** The request the user picked in the timeline, if any. */
  selected: LabRequestObservation | null;
  onSelect: (request: LabRequestObservation | null) => void;
  active: boolean;
}

function aggregateCaption(requests: LabRequestObservation[], counts: [FlowOutcome, number][]): string {
  if (requests.length === 0) return "No requests observed yet.";
  const parts = counts.map(([outcome, n]) => `${n} ${OUTCOME_LABEL[outcome].toLowerCase()}`);
  return `${requests.length} HTTP request${requests.length === 1 ? "" : "s"} (including retries): ${parts.join(", ")}.`;
}

/**
 * The request path of an experiment run: all observed requests folded together (counts per
 * component), or one request at a time (an example per outcome, or one picked in the timeline).
 */
export function RunFlow({ requests, selected, onSelect, active }: RunFlowProps) {
  const [mode, setMode] = useState<"all" | "one">("all");
  const counts = useMemo(() => {
    const map = new Map<FlowOutcome, number>();
    for (const r of requests) map.set(r.outcome, (map.get(r.outcome) ?? 0) + 1);
    return [...map.entries()].sort((a, b) => b[1] - a[1]);
  }, [requests]);
  const replicas = useMemo(() => [...new Set(requests.map((r) => r.replica).filter((r): r is string => !!r))].sort(), [requests]);

  const showOne = mode === "one" || selected !== null;
  const single = selected ?? requests.find((r) => r.outcome === counts[0]?.[0]) ?? null;
  const model = useMemo(() => {
    if (showOne && single) {
      return flowForOutcome(single.outcome, {
        replica: single.replica,
        replicas,
        status: single.status,
        requestId: !!single.request_id,
        rateHeaders: !!single.rate_policy,
        details: single.rate_remaining !== null && single.outcome !== "rate_limited" ? { redis: `${single.rate_remaining} token(s) left` } : undefined,
      });
    }
    return aggregateFlow(
      requests.map((r) => ({ outcome: r.outcome, replica: r.replica, requestId: !!r.request_id, rateHeaders: !!r.rate_policy })),
      aggregateCaption(requests, counts),
      replicas,
    );
  }, [showOne, single, requests, counts, replicas]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2" role="toolbar" aria-label="Flow view">
        <SegButton
          pressed={!showOne}
          onClick={() => {
            onSelect(null);
            setMode("all");
          }}
        >
          All requests ({requests.length})
        </SegButton>
        {counts.map(([outcome, n]) => (
          <SegButton
            key={outcome}
            pressed={showOne && single?.outcome === outcome && (!selected || selected.outcome === outcome)}
            onClick={() => {
              onSelect(requests.find((r) => r.outcome === outcome) ?? null);
              setMode("one");
            }}
          >
            {OUTCOME_LABEL[outcome]} × {n}
          </SegButton>
        ))}
        {active && <span className="text-xs text-muted-foreground">updating…</span>}
      </div>
      {showOne && single && (
        <p className="text-xs text-muted-foreground" data-testid="selected-request">
          Request #{single.index + 1}
          {single.attempt > 1 && `, attempt ${single.attempt}`} · {single.method} {single.path} · {single.status ?? "no response"}
          {single.code ? ` ${single.code}` : ""} · {single.replica ?? "unknown replica"} · {single.latency_ms} ms
        </p>
      )}
      <RequestFlowDiagram
        model={model}
        mode={showOne ? "single" : "aggregate"}
        playKey={showOne && single ? `${single.index}-${single.attempt}-${single.lane}` : "all"}
        animate={showOne}
        showAsync={requests.some((r) => r.method === "POST")}
      />
    </div>
  );
}

function SegButton({ pressed, onClick, children }: { pressed: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      aria-pressed={pressed}
      onClick={onClick}
      className={cn(
        "rounded-md border px-2.5 py-1 text-xs font-medium transition-colors outline-none hover:bg-accent focus-visible:ring-[3px] focus-visible:ring-ring/50",
        pressed && "border-primary/50 bg-primary text-primary-foreground hover:bg-primary/90",
      )}
    >
      {children}
    </button>
  );
}
