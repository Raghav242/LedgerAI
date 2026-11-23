import { CircleDashed } from "lucide-react";
import { Disclosure } from "@/components/common/Disclosure";
import { ErrorState, LoadingRows } from "@/components/common/states";
import { cn } from "@/lib/utils";
import type { TraceView } from "@/types/api";
import { buildLifecycle, type ExplorerStage, type SyncFacts } from "./lifecycleModel";
import { EVIDENCE_STYLE, busy, useTransferLifecycle } from "./useLifecycle";

function StageRow({ stage, last }: { stage: ExplorerStage; last: boolean }) {
  const style = EVIDENCE_STYLE[stage.evidence];
  const Icon = stage.evidence === "pending" ? CircleDashed : style.icon;
  return (
    <li className="relative flex gap-3 pb-3" data-stage={stage.id} data-evidence={stage.evidence}>
      {!last && <span aria-hidden className="absolute top-7 bottom-0 left-[13px] w-px bg-border" />}
      <span className={cn("mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full border", style.ring, style.text)}>
        <Icon className={cn("size-3.5", stage.evidence === "pending" && "motion-safe:animate-pulse")} aria-hidden />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline justify-between gap-x-2">
          <span className="text-sm font-medium">{stage.label}</span>
          <span className="flex items-center gap-2 text-xs">
            {stage.durationMs !== null && <span className="tabular-nums text-muted-foreground">{stage.durationMs.toFixed(stage.durationMs < 10 ? 1 : 0)} ms</span>}
            <span className={style.text}>{style.label}</span>
          </span>
        </div>
        <p className="text-xs text-muted-foreground">
          {stage.detail}
          {stage.source && <span className="text-muted-foreground/80"> · {stage.source}</span>}
        </p>
      </div>
    </li>
  );
}

export function LifecycleStages({ stages }: { stages: ExplorerStage[] }) {
  const sync = stages.filter((s) => !s.async);
  const asyncStages = stages.filter((s) => s.async);
  return (
    <div data-testid="lifecycle">
      <ol aria-label="Synchronous request stages">
        {sync.map((s, i) => (
          <StageRow key={s.id} stage={s} last={i === sync.length - 1} />
        ))}
      </ol>
      <div className="my-2 flex items-center gap-2 text-[11px] text-muted-foreground" role="separator">
        <span className="h-px flex-1 border-t border-dashed" />
        response sent · everything below happens after COMMIT, asynchronously
        <span className="h-px flex-1 border-t border-dashed" />
      </div>
      <ol aria-label="Asynchronous event stages">
        {asyncStages.map((s, i) => (
          <StageRow key={s.id} stage={s} last={i === asyncStages.length - 1} />
        ))}
      </ol>
    </div>
  );
}

function TraceTimeline({ trace }: { trace: TraceView }) {
  const spans = trace.spans.filter((s) => !s.name.endsWith("http receive") && !s.name.endsWith("http send") && s.name !== "connect");
  const total = Math.max(1, ...spans.map((s) => s.start_offset_ms + s.duration_ms));
  return (
    <div className="space-y-1" data-testid="trace-timeline">
      <p className="text-xs text-muted-foreground">
        Trace <span className="font-mono">{trace.trace_id.slice(0, 16)}…</span> · {spans.length} spans from {trace.services.join(", ")}
        {trace.truncated && " (truncated)"}
      </p>
      <ul className="space-y-0.5">
        {spans.map((s) => (
          <li key={s.span_id} className="grid grid-cols-[minmax(0,10rem)_1fr_4rem] items-center gap-2 text-[11px]">
            <span className={cn("truncate", s.error && "text-destructive")} title={`${s.service} · ${s.name}`}>
              {s.name}
            </span>
            <span className="relative h-2 rounded bg-muted/40">
              <span
                className={cn("absolute h-2 rounded", s.service.includes("api") ? "bg-primary/60" : "bg-info/60", s.error && "bg-destructive/70")}
                style={{ left: `${(s.start_offset_ms / total) * 100}%`, width: `${Math.max(0.5, (s.duration_ms / total) * 100)}%` }}
              />
            </span>
            <span className="text-right tabular-nums text-muted-foreground">{s.duration_ms.toFixed(1)} ms</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * The eight stages of one transfer: synchronous request, then the asynchronous event path.
 * Pass `sync` when this page received the response itself (Playground); omit it for history.
 */
export function LifecycleExplorer({ transferId, traceId, sync = null }: { transferId: string; traceId?: string | null; sync?: SyncFacts | null }) {
  const { lifecycle, trace, traceId: tracedId } = useTransferLifecycle(transferId, traceId);
  const traceData = trace.data ?? null;

  if (lifecycle.isLoading) return <LoadingRows rows={4} />;
  if (lifecycle.error) return <ErrorState error={lifecycle.error} onRetry={() => void lifecycle.refetch()} compact />;

  const stages = buildLifecycle({ sync, lifecycle: lifecycle.data, trace: traceData });
  return (
    <div className="space-y-3">
      <LifecycleStages stages={stages} />
      {busy(stages) && <p className="text-xs text-muted-foreground">Updating while events are delivered…</p>}
      {tracedId ? (
        <Disclosure title="Full trace" meta={traceData?.available ? `${traceData.spans.length} spans` : "not available"}>
          {traceData?.available ? (
            <TraceTimeline trace={traceData} />
          ) : (
            <p className="text-xs text-muted-foreground">{traceData?.reason ?? "Waiting for the trace store…"} The stages above use persisted state only.</p>
          )}
        </Disclosure>
      ) : (
        <p className="text-xs text-muted-foreground">No distributed trace for this transfer (tracing was off). Stages use response headers and persisted state.</p>
      )}
    </div>
  );
}
