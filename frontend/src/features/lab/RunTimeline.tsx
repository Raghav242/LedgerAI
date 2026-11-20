import { useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { LabEvent, LabRequestObservation } from "@/types/api";

const LEVEL_DOT: Record<LabEvent["level"], string> = {
  info: "bg-info",
  success: "bg-success",
  warning: "bg-warning",
  error: "bg-destructive",
};

const VISIBLE_REQUESTS = 120;

function seconds(ms: number): string {
  return `+${(ms / 1000).toFixed(2)} s`;
}

/**
 * Event log of a run: phases, notes and checks always; individual requests optionally
 * (a run can have hundreds). Clicking a request shows its path in the flow diagram.
 */
export function RunTimeline({
  events,
  dropped,
  selected,
  onSelect,
}: {
  events: LabEvent[];
  dropped: number;
  selected: LabRequestObservation | null;
  onSelect: (request: LabRequestObservation) => void;
}) {
  const [showRequests, setShowRequests] = useState(true);
  const [showAll, setShowAll] = useState(false);
  const requestCount = useMemo(() => events.filter((e) => e.kind === "request").length, [events]);

  const visible = useMemo(() => {
    if (!showRequests) return events.filter((e) => e.kind !== "request");
    if (showAll || requestCount <= VISIBLE_REQUESTS) return events;
    let kept = 0;
    return events.filter((e) => e.kind !== "request" || kept++ < VISIBLE_REQUESTS);
  }, [events, showRequests, showAll, requestCount]);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <label className="flex items-center gap-2 text-xs">
          <input type="checkbox" checked={showRequests} onChange={(e) => setShowRequests(e.target.checked)} className="accent-primary" />
          Show individual requests ({requestCount})
        </label>
        {showRequests && requestCount > VISIBLE_REQUESTS && !showAll && (
          <Button size="sm" variant="ghost" onClick={() => setShowAll(true)}>
            Show all {requestCount}
          </Button>
        )}
        {dropped > 0 && <span className="text-xs text-muted-foreground">{dropped} further request events were counted but not stored (cap).</span>}
      </div>
      <ol className="max-h-[28rem] space-y-0.5 overflow-y-auto rounded-lg border bg-muted/10 p-2" aria-label="Run timeline">
        {visible.length === 0 && <li className="px-2 py-3 text-sm text-muted-foreground">Waiting for the first events…</li>}
        {visible.map((event) =>
          event.kind === "request" && event.request ? (
            <li key={event.seq}>
              <button
                type="button"
                onClick={() => onSelect(event.request!)}
                className={cn(
                  "flex w-full items-center gap-2 rounded px-2 py-0.5 text-left font-mono text-[11px] hover:bg-accent",
                  selected && selected.index === event.request.index && selected.attempt === event.request.attempt && selected.lane === event.request.lane && "bg-accent ring-1 ring-primary/40",
                )}
                aria-label={`Show the path of ${event.title}`}
              >
                <span className={cn("size-1.5 shrink-0 rounded-full", LEVEL_DOT[event.level])} aria-hidden />
                <span className="w-16 shrink-0 text-muted-foreground">{seconds(event.t_ms)}</span>
                <span className="min-w-0 flex-1 truncate">{event.title}</span>
                <span className="shrink-0 text-muted-foreground">{event.request.replica ?? "?"}</span>
                <span className="w-20 shrink-0 text-right whitespace-nowrap text-muted-foreground">{event.request.latency_ms} ms</span>
              </button>
            </li>
          ) : (
            <li
              key={event.seq}
              data-kind={event.kind}
              className={cn("flex items-start gap-2 rounded px-2 py-1.5 text-xs", event.kind === "phase" && "mt-1 bg-card font-medium shadow-xs")}
            >
              <span className={cn("mt-1 size-2 shrink-0 rounded-full", LEVEL_DOT[event.level])} aria-hidden />
              <span className="w-16 shrink-0 font-mono text-[11px] text-muted-foreground">{seconds(event.t_ms)}</span>
              <span className="min-w-0 flex-1">
                {event.kind === "check" && <span className="text-muted-foreground">check · </span>}
                {event.title}
                {event.detail && <span className="block font-normal text-muted-foreground">{event.detail}</span>}
              </span>
            </li>
          ),
        )}
      </ol>
    </div>
  );
}
