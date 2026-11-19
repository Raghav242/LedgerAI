import { CheckCircle2, CircleDashed, XCircle } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

/**
 * A list of named invariants with pass/fail/unknown status. Used today by reconciliation;
 * designed for Phase 5B experiments ("no double spend", "exactly one transfer per key", ...).
 */
export type InvariantStatus = "pass" | "fail" | "unknown";

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
};

const LABELS: Record<InvariantStatus, string> = { pass: "Holds", fail: "Violated", unknown: "Not checked" };

export function InvariantCheckList({ items, className }: { items: Invariant[]; className?: string }) {
  return (
    <ul className={cn("divide-y rounded-lg border", className)} aria-label="Invariant checks">
      {items.map((item) => (
        <li key={item.id} className="flex items-start gap-3 px-4 py-3" data-status={item.status}>
          <span className="mt-0.5">{ICONS[item.status]}</span>
          <div className="min-w-0 flex-1 space-y-0.5">
            <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
              <span className="text-sm font-medium">{item.label}</span>
              <span
                className={cn(
                  "text-xs font-medium",
                  item.status === "pass" && "text-success",
                  item.status === "fail" && "text-destructive",
                  item.status === "unknown" && "text-muted-foreground",
                )}
              >
                {LABELS[item.status]}
              </span>
            </div>
            {item.description && <p className="text-xs text-muted-foreground">{item.description}</p>}
            {item.observed && <div className="pt-1 text-xs">{item.observed}</div>}
          </div>
        </li>
      ))}
    </ul>
  );
}
