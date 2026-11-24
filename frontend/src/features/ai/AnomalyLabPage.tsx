import { useQuery } from "@tanstack/react-query";
import { BrainCircuit, Loader2, Play } from "lucide-react";
import { useState } from "react";
import { useSearchParams } from "react-router";
import { aiApi, type AnomalyResult, type ModelInfo } from "@/api/ai";
import { isApiError } from "@/api/client";
import { Disclosure } from "@/components/common/Disclosure";
import { PageHeader } from "@/components/common/display";
import { StatusSummary } from "@/components/common/StatusSummary";
import { EmptyState, ErrorState, LoadingRows } from "@/components/common/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { LifecycleExplorer } from "@/features/observability/LifecycleExplorer";
import { isActive, useLabRun, useStartRun } from "@/features/lab/hooks";
import { cn } from "@/lib/utils";
import type { LabRun } from "@/types/api";
import { FEATURE_LABEL, SCENARIOS, featureValue, scorePosition, scoreReading, verdicts } from "./aiModel";
import { ModelEvaluation } from "./ModelEvaluation";

interface ScenarioResult {
  scenario: string;
  description: string;
  intent_unusual: boolean;
  agrees_with_intent: boolean | null;
  transfer_id: string;
  result: (Omit<AnomalyResult, "event_id" | "transfer_id" | "source_account_id" | "occurred_at"> & { commit_to_scored_ms: number | null }) | null;
}

function scenarioData(run: LabRun | undefined): ScenarioResult | null {
  const data = run?.result?.data as Partial<ScenarioResult> | undefined;
  return data && typeof data.transfer_id === "string" ? (data as ScenarioResult) : null;
}

/** Score vs threshold on one line: where the transfer sits, where the line is. Not a probability. */
function ScoreBar({ score, threshold, version }: { score: number; threshold: number; version: string }) {
  const pos = (x: number) => `${scorePosition(version, x) * 100}%`;
  return (
    <div className="space-y-1" aria-label={`Score ${score.toFixed(4)}, threshold ${threshold.toFixed(4)}`} role="img">
      <div className="relative h-2.5 rounded-full bg-muted">
        <span className="absolute top-[-4px] h-[18px] w-0.5 bg-foreground/70" style={{ left: pos(threshold) }} aria-hidden />
        <span
          className={cn("absolute top-1/2 size-3.5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-background", score >= threshold ? "bg-warning" : "bg-success")}
          style={{ left: pos(score) }}
          aria-hidden
        />
      </div>
      <div className="flex justify-between text-[11px] text-muted-foreground">
        <span>more typical</span>
        <span>model threshold {threshold.toFixed(version === "iforest-v1" ? 3 : 4)}</span>
        <span>more unusual</span>
      </div>
    </div>
  );
}

function VerdictChips({ r }: { r: Parameters<typeof verdicts>[0] }) {
  const v = verdicts(r);
  const chip = (label: string, on: boolean) => (
    <Badge variant={on ? "warning" : "outline"} className="font-normal">
      {label}: {on ? "flag" : "ok"}
    </Badge>
  );
  return (
    <span className="inline-flex flex-wrap items-center gap-1" data-testid="ai-verdicts">
      {chip("Model", v.model)}
      {chip("Rules", v.rules)}
      {v.model !== v.rules && <Badge variant="info">disagree</Badge>}
    </span>
  );
}

function Prediction({ data, model }: { data: ScenarioResult; model: ModelInfo | undefined }) {
  const r = data.result;
  if (!r) {
    return (
      <StatusSummary tone="warning" title="No model result arrived">
        The transfer committed, but the AI worker did not store a result in time. Check the AI worker on the System Topology page.
      </StatusSummary>
    );
  }
  const reference = model?.reference ?? {};
  return (
    <div className="space-y-4" data-testid="ai-prediction" data-anomaly={r.is_anomaly ? "true" : "false"}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className={cn("text-xl font-semibold", r.is_anomaly ? "text-warning" : "text-success")}>
            {r.is_anomaly ? "Anomaly detected" : "Not flagged"}
          </div>
          <p className="text-sm text-muted-foreground">
            Score <span className="font-mono">{r.score.toFixed(4)}</span>: {scoreReading(r.model_version, r.score)} · model {r.model_version} · scored{" "}
            {r.commit_to_scored_ms !== null ? `${Math.round(r.commit_to_scored_ms)} ms after the commit` : "after the commit"}
          </p>
          <div className="mt-1">
            <VerdictChips r={r} />
          </div>
        </div>
        <Badge variant={data.agrees_with_intent ? "outline" : "warning"} title="Agreement is not ground truth">
          {data.agrees_with_intent ? "Matches the scenario's intent" : "Differs from the scenario's intent"}
        </Badge>
      </div>
      <ScoreBar score={r.score} threshold={r.threshold} version={r.model_version} />
      {r.explanation.length > 0 ? (
        <ul className="list-disc space-y-0.5 pl-5 text-sm">
          {r.explanation.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-muted-foreground">No input is outside the range seen in training.</p>
      )}
      <p className="text-xs text-muted-foreground">
        The score measures how unusual the transfer is against the synthetic training data, not a probability of fraud. Rule baseline:{" "}
        {r.rule_flags.length ? r.rule_flags.join("; ") : "no rule fired"}. The final verdict is the model OR the rules (hybrid).
      </p>

      <Disclosure title="Feature values vs training data" meta={`${Object.keys(r.features).length}`}>
        <div className="overflow-x-auto rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Feature</TableHead>
                <TableHead className="text-right">This transfer</TableHead>
                <TableHead className="text-right">Typical range (5th–95th pct)</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {Object.entries(r.features).map(([name, value]) => {
                const ref = reference[name] as { p05: number; p95: number } | undefined;
                const outside = ref ? value < ref.p05 || value > ref.p95 : false;
                return (
                  <TableRow key={name}>
                    <TableCell>{FEATURE_LABEL[name] ?? name}</TableCell>
                    <TableCell className={cn("text-right tabular-nums", outside && "font-semibold text-warning")}>{featureValue(name, value)}</TableCell>
                    <TableCell className="text-right tabular-nums text-muted-foreground">
                      {ref ? `${featureValue(name, ref.p05)} – ${featureValue(name, ref.p95)}` : "—"}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>
        <p className="mt-2 text-xs text-muted-foreground">Features use only this account's transfers from BEFORE this one. Inference took {r.inference_ms} ms on CPU.</p>
      </Disclosure>
      <Disclosure title="Event path: transfer → outbox → RabbitMQ → AI worker">
        <LifecycleExplorer transferId={data.transfer_id} />
      </Disclosure>
    </div>
  );
}

/** Choose a scenario → run it through the real transfer API → see the asynchronous model verdict. */
export function AnomalyLabPage() {
  const [search, setSearch] = useSearchParams();
  const [scenario, setScenario] = useState<number>(Number(search.get("scenario") ?? 0) || 0);
  const runId = search.get("run");
  const model = useQuery({ queryKey: ["ai", "model"], queryFn: ({ signal }) => aiApi.model(signal), staleTime: Infinity });
  const recent = useQuery({ queryKey: ["ai", "results"], queryFn: ({ signal }) => aiApi.results(8, signal), refetchInterval: 10_000 });
  const start = useStartRun();
  const run = useLabRun(runId);
  const data = scenarioData(run.data);
  const running = isActive(run.data?.status) || start.isPending;
  const disabled = isApiError(model.error) && model.error.status === 404;

  return (
    <div className="space-y-6">
      <PageHeader
        title="AI Anomaly Lab"
        description="Create real activity through the transfer API, then see what the anomaly model decided, asynchronously."
      />
      {model.isLoading ? (
        <LoadingRows rows={3} />
      ) : disabled ? (
        <Card>
          <CardContent>
            <EmptyState icon={<BrainCircuit className="size-5" />} title="The AI Lab is disabled on this backend" description="It is part of the development-only surface (ENABLE_DEV_ENDPOINTS=true)." />
          </CardContent>
        </Card>
      ) : model.error || !model.data ? (
        <Card>
          <CardContent>
            <ErrorState error={model.error} onRetry={() => void model.refetch()} />
          </CardContent>
        </Card>
      ) : !model.data.available ? (
        <StatusSummary tone="warning" title="No model artifact">
          Train it with python -m app.ai.train, then rebuild the ai-worker image.
        </StatusSummary>
      ) : (
        <>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4" role="radiogroup" aria-label="Scenarios">
            {SCENARIOS.map((s) => (
              <button
                key={s.id}
                type="button"
                role="radio"
                aria-checked={scenario === s.index}
                onClick={() => setScenario(s.index)}
                data-scenario={s.id}
                className={cn(
                  "rounded-lg border bg-card px-3 py-3 text-left outline-none hover:bg-accent/50 focus-visible:ring-[3px] focus-visible:ring-ring/50",
                  scenario === s.index && "border-primary/60 bg-accent/60",
                )}
              >
                <span className="block text-sm font-semibold">{s.name}</span>
                <span className="block text-xs text-muted-foreground">{s.hint}</span>
              </button>
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <Button
              disabled={running}
              onClick={() =>
                start.mutate(
                  { experiment: "anomaly_scenario", params: { scenario } },
                  { onSuccess: (r) => setSearch({ scenario: String(scenario), run: r.id }) },
                )
              }
            >
              {running ? <Loader2 className="animate-spin" /> : <Play />} {running ? "Running…" : "Run scenario"}
            </Button>
            <span className="text-xs text-muted-foreground">
              Uses new lab accounts with simulated money · about {SCENARIOS[scenario]!.id === "new_account" ? 5 : 30} s · model {model.data.version}
            </span>
          </div>
          {start.error && <ErrorState error={start.error} compact />}

          {runId && (
            <Card>
              <CardContent className="space-y-4 p-4">
                {run.isLoading ? (
                  <LoadingRows rows={3} />
                ) : run.data && isActive(run.data.status) ? (
                  <StatusSummary tone="info" title={run.data.progress.message || "Running"} data-testid="ai-progress">
                    Building activity through NGINX, then waiting for the event to reach the AI worker.
                  </StatusSummary>
                ) : run.data?.status === "error" ? (
                  <StatusSummary tone="destructive" title="The scenario could not complete">
                    {run.data.error}
                  </StatusSummary>
                ) : data ? (
                  <>
                    <p className="text-sm" data-testid="ai-summary">
                      {run.data?.result?.summary}
                    </p>
                    <Prediction data={data} model={model.data} />
                    {run.data?.checks && (
                      <Disclosure title="System checks" meta={`${run.data.checks.filter((c) => c.status === "pass").length}/${run.data.checks.length} passed`}>
                        <ul className="space-y-1 text-sm">
                          {run.data.checks.map((c) => (
                            <li key={c.id} className={cn(c.status === "fail" && "text-destructive")}>
                              {c.status === "pass" ? "✓" : c.status === "fail" ? "✗" : "–"} {c.label} <span className="text-muted-foreground">({c.observed})</span>
                            </li>
                          ))}
                        </ul>
                      </Disclosure>
                    )}
                  </>
                ) : null}
              </CardContent>
            </Card>
          )}

          <Disclosure variant="boxed" className="bg-card" title="Model card: v1 vs v2 vs rules, drift and limitations" meta={model.data.version ?? undefined}>
            <ModelEvaluation model={model.data} />
          </Disclosure>
          <Disclosure variant="boxed" className="bg-card" title="Recently scored transfers" meta={recent.data ? String(recent.data.length) : undefined}>
            {recent.data && recent.data.length > 0 ? (
              <div className="overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Scored</TableHead>
                      <TableHead className="text-right">Score</TableHead>
                      <TableHead>Verdict</TableHead>
                      <TableHead className="hidden md:table-cell">Model · rules</TableHead>
                      <TableHead className="hidden sm:table-cell">Top reason</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {recent.data.map((r) => (
                      <TableRow key={r.event_id}>
                        <TableCell className="tabular-nums">{new Date(r.scored_at).toLocaleTimeString()}</TableCell>
                        <TableCell className="text-right font-mono">{r.score.toFixed(r.model_version === "iforest-v1" ? 3 : 4)}</TableCell>
                        <TableCell>{r.is_anomaly ? <Badge variant="warning">anomaly</Badge> : <Badge variant="outline">not flagged</Badge>}</TableCell>
                        <TableCell className="hidden md:table-cell">
                          <VerdictChips r={r} />
                        </TableCell>
                        <TableCell className="hidden max-w-[24rem] truncate text-xs text-muted-foreground sm:table-cell">{r.explanation[0] ?? "—"}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">No transfers scored yet.</p>
            )}
          </Disclosure>
        </>
      )}
    </div>
  );
}
