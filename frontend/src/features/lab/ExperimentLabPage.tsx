import { FlaskConical, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { useSearchParams } from "react-router";
import { isApiError } from "@/api/client";
import { PageHeader } from "@/components/common/display";
import { EmptyState, ErrorState, LoadingRows } from "@/components/common/states";
import { Card, CardContent } from "@/components/ui/card";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/misc";
import { describeError } from "@/lib/errors";
import type { ExperimentId } from "@/types/api";
import { ExperimentCard } from "./ExperimentCard";
import { ExperimentDetail } from "./ExperimentDetail";
import { controllerAvailability, isActive, useControllerStatus, useExperiments, useLabRuns, useStartControllerRun, useStartRun } from "./hooks";
import { RecentRuns } from "./RecentRuns";
import { RunPanel } from "./RunPanel";

const SAFETY =
  "Correctness experiments run in the API with bounded parameters, one run at a time, on their own new lab accounts with simulated money and their own rate-limit bucket. Failure experiments are different: the API cannot run them at all. Only a separate local controller (python -m labctl --enable-failures, 127.0.0.1 only) can, and it can stop and restart exactly four allow-listed local services, after an explicit confirmation, restoring them automatically.";

const GROUPS = [
  { runner: "api", title: "Correctness under load", hint: "Bounded traffic through NGINX; nothing is stopped." },
  { runner: "controller", title: "Failure & recovery", hint: "Stops one local service, then restores it. Needs the local controller." },
] as const;

/**
 * Experiment Lab: pick → configure → run → read the result → explore.
 * URL state: ?experiment=<id>&run=<run id>, so a run can be linked and reloaded.
 */
export function ExperimentLabPage() {
  const [search, setSearch] = useSearchParams();
  const experiments = useExperiments();
  const runs = useLabRuns();
  const start = useStartRun();
  const startFailure = useStartControllerRun();
  const [safetyOpen, setSafetyOpen] = useState(false);

  const list = experiments.data ?? [];
  const selectedId = (search.get("experiment") as ExperimentId | null) ?? list[0]?.id ?? null;
  const selected = list.find((e) => e.id === selectedId) ?? list[0];
  const runId = search.get("run");
  const activeRun = runs.data?.find((r) => isActive(r.status));

  function update(next: { experiment?: string; run?: string | null }) {
    setSearch((params) => {
      const copy = new URLSearchParams(params);
      if (next.experiment) copy.set("experiment", next.experiment);
      if (next.run === null) copy.delete("run");
      else if (next.run) copy.set("run", next.run);
      return copy;
    });
  }

  const infrastructure = selected?.runner === "controller";
  const controller = useControllerStatus(infrastructure);
  const availability = controllerAvailability(controller);
  const starter = infrastructure ? startFailure : start;
  const labDisabled = isApiError(experiments.error) && experiments.error.status === 404;
  const conflict = isApiError(starter.error) && (starter.error.code === "LAB_RUN_IN_PROGRESS" || starter.error.status === 409);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Experiment Lab"
        description="Run a controlled experiment against the real stack and see what happened."
        actions={
          <button
            type="button"
            aria-expanded={safetyOpen}
            aria-controls="lab-safety"
            onClick={() => setSafetyOpen((o) => !o)}
            className="inline-flex items-center gap-1.5 rounded-md border border-success/30 bg-success/10 px-2 py-1 text-xs font-medium text-success outline-none hover:bg-success/15 focus-visible:ring-[3px] focus-visible:ring-ring/50"
          >
            <ShieldCheck className="size-3.5" aria-hidden /> Safe · local only
          </button>
        }
      />
      {safetyOpen && (
        <p id="lab-safety" className="rounded-lg border border-success/30 bg-success/5 px-4 py-3 text-sm text-muted-foreground">
          {SAFETY}
        </p>
      )}

      {experiments.isLoading ? (
        <LoadingRows rows={4} />
      ) : labDisabled ? (
        <Card>
          <CardContent>
            <EmptyState
              icon={<FlaskConical className="size-5" />}
              title="The Experiment Lab is disabled on this backend"
              description="It is part of the development-only surface. Set ENABLE_DEV_ENDPOINTS=true in .env and restart the stack (docker compose up -d)."
            />
          </CardContent>
        </Card>
      ) : experiments.error ? (
        <Card>
          <CardContent>
            <ErrorState error={experiments.error} onRetry={() => void experiments.refetch()} />
          </CardContent>
        </Card>
      ) : (
        <>
          {GROUPS.map((group) => {
            const items = list.filter((e) => (e.runner ?? "api") === group.runner);
            if (!items.length) return null;
            return (
              <section key={group.runner} aria-label={group.title} className="space-y-2">
                <div className="flex flex-wrap items-baseline gap-x-2">
                  <h2 className="text-sm font-semibold">{group.title}</h2>
                  <span className="text-xs text-muted-foreground">{group.hint}</span>
                </div>
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4" role="list" aria-label={group.runner === "api" ? "Experiments" : "Failure experiments"}>
                  {items.map((experiment) => (
                    <div role="listitem" key={experiment.id}>
                      <ExperimentCard
                        experiment={experiment}
                        selected={experiment.id === selected?.id}
                        onSelect={() => {
                          start.reset();
                          startFailure.reset();
                          update({ experiment: experiment.id });
                        }}
                      />
                    </div>
                  ))}
                </div>
              </section>
            );
          })}

          {/* DOM order is config → result → history, so on narrow screens the result comes
              before the history; on wide screens history sits under the config. */}
          <div className="grid grid-cols-1 items-start gap-6 lg:grid-cols-12 lg:grid-rows-[auto_1fr]">
            <div className="space-y-4 lg:col-span-5 xl:col-span-4">
              {selected && (
                <ExperimentDetail
                  key={selected.id}
                  experiment={selected}
                  starting={starter.isPending}
                  availability={infrastructure ? availability : undefined}
                  blockedReason={activeRun ? "Another run is in progress. Runs never overlap, so they cannot distort each other." : null}
                  onRun={(params) =>
                    starter.mutate({ experiment: selected.id, params }, { onSuccess: (run) => update({ run: run.id, experiment: run.experiment }) })
                  }
                />
              )}
              {starter.error && (
                <Alert variant={conflict ? "warning" : "destructive"}>
                  <FlaskConical />
                  <AlertTitle>{conflict ? "Another run is in progress" : describeError(starter.error).title}</AlertTitle>
                  <AlertDescription>
                    {describeError(starter.error).message}
                    {conflict && activeRun && (
                      <button
                        type="button"
                        className="font-medium underline underline-offset-2"
                        onClick={() => update({ run: activeRun.id, experiment: activeRun.experiment })}
                      >
                        Open the active run
                      </button>
                    )}
                  </AlertDescription>
                </Alert>
              )}
            </div>

            <div className="min-w-0 lg:col-span-7 lg:row-span-2 xl:col-span-8">
              {runId ? (
                <RunPanel key={runId} runId={runId} experiments={list} />
              ) : (
                <Card>
                  <CardContent>
                    <EmptyState
                      icon={<FlaskConical className="size-5" />}
                      title="No run selected"
                      description="Press Run experiment, or open a run from the history."
                    />
                  </CardContent>
                </Card>
              )}
            </div>

            <div className="lg:col-span-5 lg:col-start-1 xl:col-span-4">
              <RecentRuns selectedRunId={runId} onOpen={(id, experiment) => update({ run: id, experiment })} />
            </div>
          </div>
        </>
      )}
    </div>
  );
}
