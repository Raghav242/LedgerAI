import { CheckCircle2, ChevronRight, CircleDashed, MinusCircle, XCircle } from "lucide-react";
import { useState, type ReactNode } from "react";
import { cn } from "@/lib/utils";

/**
 * Named invariants with pass/fail/unknown/not-applicable status (reconciliation, experiment
 * runs). Each row is compact; the explanation and the observed value open on demand.
 * Violated checks start expanded: that is when the detail matters.
 */
export type InvariantStatus = "pass" | "fail" | "unknown" | "skipped";

export interface Invariant {
  id: string;
  label: string;
  description?: ReactNode;
  status: InvariantStatus;
  observed?: ReactNode;
}

const ICONS: Record<InvariantStatus, ReactNode> = {
  pass: <CheckCircle2 className="size-4 text-success" aria-hidden />,
  fail: <XCircle className="size-4 text-destructive" aria-hidden />,
  unknown: <CircleDashed className="size-4 text-muted-foreground" aria-hidden />,
  skipped: <MinusCircle className="size-4 text-muted-foreground" aria-hidden />,
};

const LABELS: Record<InvariantStatus, string> = {
  pass: "Holds",
  fail: "Violated",
  unknown: "Not checked",
  // The check does not apply to this run (e.g. "spread across replicas" with one replica).
  skipped: "Not applicable",
};

export function InvariantCheckList({ items, className, label = "Invariant checks" }: { items: Invariant[]; className?: string; label?: string }) {
  return (
    <ul className={cn("divide-y rounded-lg border", className)} aria-label={label}>
      {items.map((item) => (
        <InvariantRow key={item.id} item={item} />
      ))}
    </ul>
  );
}

function InvariantRow({ item }: { item: Invariant }) {
  const hasDetail = !!(item.description || item.observed);
  const [open, setOpen] = useState(item.status === "fail");
  const detailId = `invariant-${item.id}`;
  return (
    <li data-status={item.status}>
      <button
        type="button"
        disabled={!hasDetail}
        aria-expanded={hasDetail ? open : undefined}
        aria-controls={hasDetail ? detailId : undefined}
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-3 px-4 py-2.5 text-left outline-none hover:bg-accent/40 focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:ring-inset disabled:cursor-default disabled:hover:bg-transparent"
      >
        {ICONS[item.status]}
        <span className="min-w-0 flex-1 text-sm">{item.label}</span>
        <span
          className={cn(
            "shrink-0 text-xs font-medium",
            item.status === "pass" && "text-success",
            item.status === "fail" && "text-destructive",
            (item.status === "unknown" || item.status === "skipped") && "text-muted-foreground",
          )}
        >
          {LABELS[item.status]}
        </span>
        {hasDetail && (
          <ChevronRight className={cn("size-3.5 shrink-0 text-muted-foreground transition-transform motion-reduce:transition-none", open && "rotate-90")} aria-hidden />
        )}
      </button>
      {hasDetail && open && (
        <div id={detailId} className="space-y-1 px-4 pb-3 pl-11">
          {item.description && <p className="text-xs text-muted-foreground">{item.description}</p>}
          {item.observed && <div className="text-xs">{item.observed}</div>}
        </div>
      )}
    </li>
  );
}

/** "8 of 8 checks passed" with one icon; the summary line above a check list. */
export function InvariantSummary({ items }: { items: Invariant[] }) {
  const applicable = items.filter((i) => i.status !== "skipped");
  const passed = applicable.filter((i) => i.status === "pass").length;
  const failed = applicable.filter((i) => i.status === "fail").length;
  const skipped = items.length - applicable.length;
  return (
    <p className="flex items-center gap-2 text-sm" data-testid="invariant-summary">
      {failed ? ICONS.fail : ICONS.pass}
      <span className={cn("font-medium", failed ? "text-destructive" : "text-foreground")}>
        {failed ? `${failed} of ${applicable.length} checks violated` : `${passed} of ${applicable.length} checks passed`}
      </span>
      {skipped > 0 && <span className="text-xs text-muted-foreground">· {skipped} not applicable</span>}
    </p>
  );
}
