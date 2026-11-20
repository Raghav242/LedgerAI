import { AlertTriangle } from "lucide-react";
import { useMemo, useState } from "react";
import { IdText, KeyValue } from "@/components/common/display";
import { ErrorState, LoadingRows } from "@/components/common/states";
import { CompactMetric } from "@/components/common/StatusSummary";
import { Tabs } from "@/components/common/Tabs";
import { InvariantCheckList, InvariantSummary, type Invariant } from "@/components/lab/InvariantCheckList";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/misc";
import { tabPanelProps } from "@/lib/a11y";
import { formatDateTime, formatRelative } from "@/lib/format";
import type { ExperimentDefinition, ExperimentId, LabMetric, LabRequestObservation, LabRun } from "@/types/api";
import { EXPERIMENT_COPY } from "./experimentIcons";
import { GuidedExplanation } from "./GuidedExplanation";
import { isActive, useLabRun, useRunEvents } from "./hooks";
import { MetricsGrid } from "./MetricsGrid";
import { ExperimentResultView, ResultVisual } from "./ResultViews";
import { RunFlow } from "./RunFlowCard";
import { RunStatusBadge } from "./RunStatusBadge";
import { RunTimeline } from "./RunTimeline";

/** The three or four numbers that answer "what happened?" for each experiment. */
const KEY_METRICS: Record<ExperimentId, string[]> = {
  concurrency: ["total", "succeeded", "rejected", "unexpected"],
  idempotency: ["duplicates", "committed", "replays", "key_conflicts"],
  rate_limiting: ["burst", "allowed", "rejected", "probe_allowed"],
  load_balancing: ["total", "replicas", "spread", "p50"],
};

type TabId = "flow" | "checks" | "timeline" | "details";

function duration(run: LabRun): string | null {
  if (!run.started_at) return null;
  const end = run.finished_at ? new Date(run.finished_at).getTime() : Date.now();
  return `${((end - new Date(run.started_at).getTime()) / 1000).toFixed(1)} s`;
}

function keyMetrics(run: LabRun): LabMetric[] {
  const all = run.result?.metrics ?? [];
  const picked = KEY_METRICS[run.experiment].map((key) => all.find((m) => m.key === key)).filter((m): m is LabMetric => !!m);
  return picked.length ? picked : all.slice(0, 4);
}

/**
 * One run. First what happened (status, one sentence, a few numbers, one picture); then, on
 * demand, the request flow, every check, the full timeline and the technical details.
 */
export function RunPanel({ runId, experiments }: { runId: string; experiments: ExperimentDefinition[] }) {
  const runQuery = useLabRun(runId);
  const eventsQuery = useRunEvents(runId);
  const [selected, setSelected] = useState<LabRequestObservation | null>(null);
  const [tab, setTab] = useState<TabId>("flow");
  const events = useMemo(() => eventsQuery.data?.events ?? [], [eventsQuery.data]);
  const requests = useMemo(() => events.filter((e) => e.kind === "request" && e.request).map((e) => e.request!), [events]);

  if (runQuery.isLoading) return <LoadingRows rows={4} />;
  if (runQuery.error || !runQuery.data) {
    return (
      <Card>
        <CardContent>
          <ErrorState error={runQuery.error} onRetry={() => void runQuery.refetch()} />
        </CardContent>
      </Card>
    );
  }

  const run = runQuery.data;
  const definition = experiments.find((e) => e.id === run.experiment);
  const active = isActive(run.status);
  const progress = run.progress;
  const percent = progress.total > 0 ? Math.min(100, Math.round((progress.completed / progress.total) * 100)) : null;
  const invariants: Invariant[] = (run.checks ?? []).map((c) => ({
    id: c.id,
    label: c.label,
    status: c.status === "pass" ? "pass" : c.status === "fail" ? "fail" : "skipped",
    description: c.description ?? undefined,
    observed: <span className="font-mono text-[11px] text-muted-foreground">{c.observed}</span>,
  }));
  const failedChecks = invariants.filter((c) => c.status === "fail");
  const applicable = invariants.filter((c) => c.status !== "skipped");
  const passedCount = applicable.filter((c) => c.status === "pass").length;

  const showRequest = (request: LabRequestObservation | null) => {
    setSelected(request);
    if (request) setTab("flow");
  };

  return (
    <div className="space-y-4" data-testid="run-panel" data-run-id={run.id} data-status={run.status}>
      {/* --- 1. What happened ------------------------------------------------------------- */}
      <Card className="gap-4 py-4" aria-live="polite">
        <CardContent className="space-y-4 px-4">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <h2 className="text-base font-semibold">{EXPERIMENT_COPY[run.experiment].name}</h2>
            <RunStatusBadge status={run.status} />
            <span className="ml-auto text-xs text-muted-foreground">
              {formatRelative(run.created_at)}
              {duration(run) && ` · ${duration(run)}`}
            </span>
          </div>

          {active && (
            <div className="space-y-1.5" data-testid="run-progress">
              <div className="flex items-center justify-between gap-2 text-sm">
                <span>{progress.message || progress.phase}</span>
                {percent !== null && (
                  <span className="font-mono text-xs text-muted-foreground">
                    {progress.completed}/{progress.total}
                  </span>
                )}
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-muted" role="progressbar" aria-label="Run progress" aria-valuenow={percent ?? undefined} aria-valuemin={0} aria-valuemax={100}>
                <div
                  className={percent === null ? "h-full w-1/3 rounded-full bg-info motion-safe:animate-pulse" : "h-full rounded-full bg-info transition-[width] duration-300"}
                  style={percent === null ? undefined : { width: `${percent}%` }}
                />
              </div>
            </div>
          )}

          {run.status === "error" && run.error && (
            <Alert variant="warning">
              <AlertTriangle />
              <AlertTitle>The run did not complete</AlertTitle>
              <AlertDescription>{run.error}</AlertDescription>
            </Alert>
          )}
          {run.status === "failed" && (
            <Alert variant="destructive">
              <AlertTriangle />
              <AlertTitle>
                {failedChecks.length} invariant{failedChecks.length === 1 ? "" : "s"} violated
              </AlertTitle>
              <AlertDescription>{failedChecks.map((c) => c.label).join(" · ")}</AlertDescription>
            </Alert>
          )}

          {run.result && (
            <>
              <p className="text-sm" data-testid="run-summary">
                {run.result.summary}
              </p>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-2 xl:grid-cols-4" aria-label="Key results">
                {keyMetrics(run).map((m) => (
                  <CompactMetric
                    key={m.key}
                    data-metric={m.key}
                    label={m.label}
                    value={
                      <>
                        {m.value}
                        {m.unit && <span className="ml-1 text-xs font-normal text-muted-foreground">{m.unit}</span>}
                      </>
                    }
                    tone={m.tone === "neutral" ? undefined : m.tone}
                  />
                ))}
              </div>
              <ResultVisual run={run} />
            </>
          )}
        </CardContent>
      </Card>

      <GuidedExplanation run={run} experiment={definition} />

      {/* --- 2. Explore on demand ------------------------------------------------------------ */}
      <Card className="gap-0 py-0">
        <Tabs<TabId>
          idPrefix={`run-${run.id}`}
          label="Run details"
          value={tab}
          onChange={setTab}
          className="px-2 pt-1"
          items={[
            { id: "flow", label: "Request flow" },
            { id: "checks", label: "Checks", meta: run.checks ? `${passedCount}/${applicable.length}` : undefined },
            { id: "timeline", label: "Timeline", meta: run.event_count || undefined },
            { id: "details", label: "Technical details" },
          ]}
        />
        <div {...tabPanelProps(`run-${run.id}`, tab)} className="p-4 outline-none">
          {tab === "flow" && <RunFlow requests={requests} selected={selected} onSelect={setSelected} active={active} />}

          {tab === "checks" &&
            (run.checks ? (
              <div className="space-y-3">
                <InvariantSummary items={invariants} />
                <InvariantCheckList items={invariants} label="Experiment invariants" />
                {invariants.some((c) => c.status === "skipped") && (
                  <p className="text-xs text-muted-foreground">“Not applicable” checks need more than one replica to mean anything, so they are not counted.</p>
                )}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">Checks run when the experiment finishes.</p>
            ))}

          {tab === "timeline" &&
            (eventsQuery.error ? (
              <ErrorState error={eventsQuery.error} onRetry={() => void eventsQuery.refetch()} compact />
            ) : (
              <RunTimeline events={events} dropped={eventsQuery.data?.dropped ?? 0} selected={selected} onSelect={showRequest} />
            ))}

          {tab === "details" && (
            <div className="space-y-5">
              <dl className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-4">
                <KeyValue label="Run id">
                  <IdText id={run.id} />
                </KeyValue>
                <KeyValue label="Started">
                  <span className="text-xs">{run.started_at ? formatDateTime(run.started_at) : "—"}</span>
                </KeyValue>
                <KeyValue label="Duration">
                  <span className="font-mono text-xs">{duration(run) ?? "—"}</span>
                </KeyValue>
                <KeyValue label="Runner → traffic target">
                  <span className="block truncate font-mono text-xs" title={run.target_base_url}>
                    {run.runner_replica} → {run.target_base_url.replace(/^https?:\/\//, "")}
                  </span>
                </KeyValue>
              </dl>
              <div className="flex flex-wrap gap-1.5" aria-label="Parameters">
                {Object.entries(run.params).map(([name, value]) => {
                  const spec = definition?.params.find((p) => p.name === name);
                  return (
                    <Badge key={name} variant="outline" className="font-mono font-normal">
                      {spec?.label ?? name}: {spec?.unit === "cents" ? `$${(value / 100).toFixed(2)}` : value}
                      {spec?.unit === "seconds" ? " s" : ""}
                    </Badge>
                  );
                })}
              </div>
              {run.result && (
                <>
                  <MetricsGrid metrics={run.result.metrics} />
                  <ExperimentResultView run={run} />
                </>
              )}
            </div>
          )}
        </div>
      </Card>
    </div>
  );
}
