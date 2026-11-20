import { cn } from "@/lib/utils";
import type { LabMetric } from "@/types/api";

const TONE: Record<NonNullable<LabMetric["tone"]>, string> = {
  neutral: "",
  success: "text-success",
  warning: "text-warning",
  destructive: "text-destructive",
  info: "text-info",
};

/** Summary numbers reported by the backend for a finished run. */
export function MetricsGrid({ metrics, className }: { metrics: LabMetric[]; className?: string }) {
  return (
    <dl className={cn("grid grid-cols-2 gap-px overflow-hidden rounded-lg border bg-border sm:grid-cols-4", className)} aria-label="Run metrics">
      {metrics.map((m) => (
        <div key={m.key} className="flex min-w-0 flex-col gap-0.5 bg-card px-3 py-2.5" data-metric={m.key}>
          <dt className="truncate text-xs text-muted-foreground" title={m.label}>
            {m.label}
          </dt>
          <dd className={cn("tabular text-lg font-semibold tracking-tight", TONE[m.tone ?? "neutral"])}>
            {m.value}
            {m.unit && <span className="ml-1 text-xs font-normal text-muted-foreground">{m.unit}</span>}
          </dd>
          {m.hint && <dd className="truncate text-[11px] text-muted-foreground">{m.hint}</dd>}
        </div>
      ))}
    </dl>
  );
}
