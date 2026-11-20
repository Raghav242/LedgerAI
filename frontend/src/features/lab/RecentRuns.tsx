import { CheckCircle2, CircleDashed, Loader2, OctagonAlert, XCircle } from "lucide-react";
import type { ReactNode } from "react";
import { Disclosure } from "@/components/common/Disclosure";
import { ErrorState, LoadingRows } from "@/components/common/states";
import { formatRelative } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { LabRunStatus } from "@/types/api";
import { EXPERIMENT_COPY } from "./experimentIcons";
import { useLabRuns } from "./hooks";

const STATUS: Record<LabRunStatus, { label: string; icon: ReactNode; text: string }> = {
  queued: { label: "Queued", icon: <CircleDashed />, text: "text-muted-foreground" },
  running: { label: "Running", icon: <Loader2 className="motion-safe:animate-spin" />, text: "text-info" },
  passed: { label: "Passed", icon: <CheckCircle2 />, text: "text-success" },
  failed: { label: "Failed", icon: <XCircle />, text: "text-destructive" },
  error: { label: "Error", icon: <OctagonAlert />, text: "text-warning" },
};

function wideScreen(): boolean {
  return typeof window === "undefined" || typeof window.matchMedia !== "function" || window.matchMedia("(min-width: 1024px)").matches;
}

/** Compact, collapsible run history (stored in PostgreSQL; the newest 50 are kept). */
export function RecentRuns({ selectedRunId, onOpen }: { selectedRunId: string | null; onOpen: (runId: string, experiment: string) => void }) {
  const runs = useLabRuns();
  return (
    <Disclosure variant="boxed" title="Run history" meta={runs.data ? runs.data.length : undefined} defaultOpen={wideScreen()}>
      {runs.isLoading ? (
        <LoadingRows rows={3} />
      ) : runs.error ? (
        <ErrorState error={runs.error} onRetry={() => void runs.refetch()} compact />
      ) : !runs.data?.length ? (
        <p className="text-sm text-muted-foreground">No runs yet.</p>
      ) : (
        <ul className="-mx-2 max-h-80 space-y-0.5 overflow-y-auto" aria-label="Recent runs">
          {runs.data.map((run) => {
            const status = STATUS[run.status];
            return (
              <li key={run.id}>
                <button
                  type="button"
                  onClick={() => onOpen(run.id, run.experiment)}
                  aria-current={run.id === selectedRunId ? "true" : undefined}
                  className={cn(
                    "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm outline-none hover:bg-accent focus-visible:ring-[3px] focus-visible:ring-ring/50",
                    run.id === selectedRunId && "bg-accent",
                  )}
                >
                  <span className={cn("shrink-0 [&>svg]:size-3.5", status.text)} aria-hidden>
                    {status.icon}
                  </span>
                  <span className="min-w-0 flex-1 truncate">{EXPERIMENT_COPY[run.experiment].name}</span>
                  <span className={cn("shrink-0 text-xs", status.text)} data-testid="history-status" data-status={run.status}>
                    {status.label}
                  </span>
                  <span className="w-20 shrink-0 text-right text-xs text-muted-foreground">{formatRelative(run.created_at)}</span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </Disclosure>
  );
}
