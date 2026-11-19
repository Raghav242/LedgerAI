import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useReadiness } from "@/hooks/queries";
import { formatTime } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * API connectivity from GET /health/ready through NGINX. Shows exactly what the backend
 * reported (database, Redis, answering replica); nothing is inferred.
 */
export function ConnectionStatus() {
  const { data, error, isLoading, dataUpdatedAt } = useReadiness();

  let tone: "ok" | "degraded" | "down" | "pending" = "pending";
  let label = "Checking API…";
  if (error) {
    tone = "down";
    label = "API unreachable";
  } else if (data) {
    tone = data.report.status === "ok" ? "ok" : "degraded";
    label = tone === "ok" ? "API connected" : "API degraded";
  }

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          className="inline-flex items-center gap-2 rounded-md border px-2.5 py-1 text-xs text-muted-foreground outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50"
          aria-label={label}
        >
          <span
            className={cn(
              "size-2 rounded-full",
              tone === "ok" && "bg-success",
              tone === "degraded" && "bg-warning",
              tone === "down" && "bg-destructive",
              tone === "pending" && "animate-pulse bg-muted-foreground",
            )}
            aria-hidden
          />
          <span className="hidden sm:inline">{isLoading ? "Checking…" : label}</span>
        </button>
      </TooltipTrigger>
      <TooltipContent align="end" className="space-y-1">
        <div className="font-medium">GET /health/ready</div>
        {data ? (
          <>
            <div>Database: {data.report.database ?? "n/a"}</div>
            <div>Redis: {data.report.redis ?? "n/a"}</div>
            <div>Answered by: {data.report.replica ?? data.meta?.servedBy ?? "unknown"}</div>
            <div className="text-muted-foreground">Checked {formatTime(dataUpdatedAt)} · every 15 s</div>
          </>
        ) : error ? (
          <div>No response from the gateway. Start the backend: docker compose up -d --wait</div>
        ) : (
          <div>Waiting for first response…</div>
        )}
      </TooltipContent>
    </Tooltip>
  );
}
