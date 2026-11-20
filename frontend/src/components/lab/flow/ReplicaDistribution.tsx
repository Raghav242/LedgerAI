import { Split } from "lucide-react";
import { cn } from "@/lib/utils";

/** Fixed colours per replica (validated chart tokens first), so api-1 is always the same colour. */
const REPLICA_COLORS = ["bg-chart-1", "bg-chart-2", "bg-warning", "bg-info"] as const;

function replicaColor(name: string, names: string[]): string {
  const index = names.indexOf(name);
  return index >= 0 ? (REPLICA_COLORS[index % REPLICA_COLORS.length] ?? "bg-muted-foreground") : "bg-muted-foreground";
}

/**
 * NGINX fan-out for a load-balancing run: one bar per replica plus the request-by-request
 * sequence, so the round-robin pattern is visible at a glance.
 */
export function ReplicaDistribution({
  sequence,
  counts,
  className,
  showSequence = true,
}: {
  sequence: string[];
  counts: Record<string, number>;
  className?: string;
  /** The request-by-request strip; hidden in compact summaries. */
  showSequence?: boolean;
}) {
  const names = Object.keys(counts).sort();
  const total = sequence.length || 1;
  const max = Math.max(1, ...Object.values(counts));
  return (
    <div className={cn("space-y-4", className)} data-testid="replica-distribution">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-stretch">
        <div className="flex shrink-0 items-center gap-2 rounded-lg border bg-card px-3 py-2 sm:flex-col sm:justify-center">
          <Split className="size-4 text-muted-foreground" aria-hidden />
          <div className="text-xs font-semibold">NGINX</div>
          <div className="font-mono text-[11px] text-muted-foreground">{sequence.length} requests</div>
        </div>
        <ul className="min-w-0 flex-1 space-y-2" aria-label="Requests per replica">
          {names.map((name) => {
            const n = counts[name] ?? 0;
            return (
              <li key={name} className="grid grid-cols-[4.5rem_1fr_5.5rem] items-center gap-3 text-sm" data-replica={name} data-count={n}>
                <span className="flex items-center gap-1.5 font-mono text-xs">
                  <span className={cn("size-2.5 rounded-sm", replicaColor(name, names))} aria-hidden />
                  {name}
                </span>
                <div className="h-3 overflow-hidden rounded-full bg-muted">
                  <div className={cn("h-full rounded-full transition-[width] duration-500", replicaColor(name, names))} style={{ width: `${(n / max) * 100}%` }} />
                </div>
                <span className="text-right font-mono text-xs tabular">
                  {n} · {Math.round((n / total) * 100)}%
                </span>
              </li>
            );
          })}
        </ul>
      </div>
      {showSequence && (
        <div>
          <div className="mb-1.5 text-xs font-medium text-muted-foreground">Request order (each square is one request, left to right)</div>
          <ol className="flex flex-wrap gap-1" aria-label="Replica that served each request, in order">
            {sequence.map((name, i) => (
              <li
                key={i}
                title={`#${i + 1} → ${name}`}
                aria-label={`Request ${i + 1}: ${name}`}
                className={cn(
                  "flex size-5 items-center justify-center rounded-[4px] font-mono text-[9px] font-semibold text-background",
                  replicaColor(name, names),
                )}
              >
                {name.replace(/^api-/, "")}
              </li>
            ))}
          </ol>
        </div>
      )}
    </div>
  );
}
