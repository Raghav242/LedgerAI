import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight, Compass, ExternalLink, Lock } from "lucide-react";
import { Link, useSearchParams } from "react-router";
import { API_V1, http } from "@/api/client";
import { transfersApi } from "@/api/transfers";
import { PageHeader } from "@/components/common/display";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { controllerAvailability, useControllerStatus, useLabRuns } from "@/features/lab/hooks";
import { formatMinor } from "@/lib/money";
import { formatRelative } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { components } from "@/types/openapi.gen";
import type { ExperimentId, LabRunSummary } from "@/types/api";

type BenchmarkList = components["schemas"]["BenchmarkList"];

interface Step {
  title: string;
  why: string;
  to: string;
  linkLabel: string;
  defaults: string;
  expected: string;
  experiment?: ExperimentId;
  needsController?: boolean;
  source: "transfer" | "run" | "benchmark";
}

/** The seven scenarios worth showing in a 15-20 minute walkthrough, in order. */
const STEPS: Step[] = [
  {
    title: "A basic transfer",
    why: "One transfer moves money atomically: balances, two ledger entries, the idempotency outcome and an outbox event commit together.",
    to: "/playground",
    linkLabel: "Open the Transaction Playground",
    defaults: "Create two accounts, add $100 of simulated funds, transfer $25.50.",
    expected: "201 Created; open 'Request path' and then 'Full lifecycle' to follow it to the consumers.",
    source: "transfer",
  },
  {
    title: "Concurrent withdrawals",
    why: "Row locks in a fixed order make a balance impossible to overspend, however many requests race.",
    to: "/experiments?experiment=concurrency",
    linkLabel: "Open the experiment",
    defaults: "100 concurrent $10 transfers from $100 (the defaults).",
    expected: "Exactly 10 succeed, 90 get 409 INSUFFICIENT_FUNDS, the ledger reconciles.",
    experiment: "concurrency",
    source: "run",
  },
  {
    title: "Idempotent retries",
    why: "An Idempotency-Key makes retries safe: the stored outcome is replayed instead of executing again.",
    to: "/experiments?experiment=idempotency",
    linkLabel: "Open the experiment",
    defaults: "20 concurrent copies of one $10 transfer with one key.",
    expected: "One execution, 19 replays with identical bodies; reusing the key with another amount is rejected (422).",
    experiment: "idempotency",
    source: "run",
  },
  {
    title: "Distributed rate limiting",
    why: "One token bucket in Redis enforces a single quota across every replica, atomically.",
    to: "/experiments?experiment=rate_limiting",
    linkLabel: "Open the experiment",
    defaults: "A burst of 50 writes against the real write quota (20 burst + 10/s).",
    expected: "About 20 admitted across both replicas, the rest 429 with Retry-After; tokens return after the wait.",
    experiment: "rate_limiting",
    source: "run",
  },
  {
    title: "RabbitMQ outage and recovery",
    why: "The transactional outbox means transfers never depend on the broker, and no event is lost while it is down.",
    to: "/experiments?experiment=rabbitmq_outage",
    linkLabel: "Open the failure experiment",
    defaults: "3 transfers during a 5 s outage (needs python -m labctl --enable-failures).",
    expected: "Transfers commit; events wait in the outbox; after the restart each is delivered exactly once.",
    experiment: "rabbitmq_outage",
    needsController: true,
    source: "run",
  },
  {
    title: "AI anomaly detection",
    why: "An Isolation Forest scores every transfer asynchronously, off the request path, from the account's earlier activity only.",
    to: "/anomalies?scenario=1",
    linkLabel: "Open the AI Lab",
    defaults: "Scenario 'Unusually large': a $900 transfer after a ~$30 habit.",
    expected: "The transfer commits first; the verdict arrives a fraction of a second later. The model card shows its weaknesses too.",
    experiment: "anomaly_scenario",
    source: "run",
  },
  {
    title: "Performance comparison",
    why: "Stateless replicas add throughput, but on one machine they share CPUs: scaling is real and sub-linear.",
    to: "/performance",
    linkLabel: "Open the Performance Lab",
    defaults: "Recorded Locust runs with 1, 2 and 4 replicas.",
    expected: "Throughput rises with replicas at less than linear efficiency, except a hot account, which plateaus on one row lock; the page says why, from the numbers.",
    source: "benchmark",
  },
];

function Observed({ step, runs, controllerOk }: { step: Step; runs: LabRunSummary[] | undefined; controllerOk: boolean }) {
  const transfer = useQuery({
    queryKey: ["demo", "latest-transfer"],
    queryFn: ({ signal }) => transfersApi.list({ kind: "transfer", limit: 1, offset: 0 }, signal),
    enabled: step.source === "transfer",
  });
  const bench = useQuery({
    queryKey: ["performance", "benchmarks"],
    queryFn: async ({ signal }) => (await http.get<BenchmarkList>(`${API_V1}/performance/benchmarks`, { signal })).data,
    enabled: step.source === "benchmark",
  });
  let text: string | null = null;
  let tone: "ok" | "bad" | "none" = "none";
  if (step.source === "transfer") {
    const t = transfer.data?.items[0];
    if (t) {
      text = `Most recent transfer in the system: ${formatMinor(t.amount_minor)}, ${formatRelative(t.created_at)}.`;
      tone = "ok";
    }
  } else if (step.source === "run") {
    const run = runs?.find((r) => r.experiment === step.experiment && r.finished_at);
    if (run) {
      text = `${run.status === "passed" ? "Passed" : run.status === "failed" ? "Failed" : "Did not complete"} ${formatRelative(run.finished_at!)}${
        run.summary ? `: ${run.summary}` : ""
      }`;
      tone = run.status === "passed" ? "ok" : "bad";
    }
  } else {
    const run = bench.data?.runs.find((r) => !r.error && r.results.length);
    const four = run?.results.find((r) => r.label === "scale-4-MixedTransferUser");
    const one = run?.results.find((r) => r.label === "scale-1-MixedTransferUser");
    if (run) {
      text = `Recorded ${run.started_at ? new Date(run.started_at).toLocaleDateString() : run.id}${
        one && four ? `: mixed workload ${Math.round(one.rps)} req/s with 1 replica, ${Math.round(four.rps)} with 4` : ""
      }.`;
      tone = "ok";
    }
  }
  return (
    <div className="rounded-md border px-3 py-2 text-sm" data-testid="demo-observed">
      <div className="text-xs font-medium text-muted-foreground">Actual observed result · previously recorded, not run by this guide</div>
      <p className={cn(tone === "bad" && "text-destructive", tone === "none" && "text-muted-foreground")}>
        {text ?? (step.needsController && !controllerOk ? "Not run here: this step needs the local experiment controller." : "Not run recently. Open it and run it with the defaults.")}
      </p>
    </div>
  );
}

/** A lightweight walkthrough: what to show, where, what to expect, and what was last observed. */
export function GuidedDemoPage() {
  const [search, setSearch] = useSearchParams();
  const index = Math.min(STEPS.length - 1, Math.max(0, Number(search.get("step") ?? 1) - 1 || 0));
  const step = STEPS[index]!;
  const runs = useLabRuns();
  const controller = useControllerStatus(!!step.needsController);
  const availability = controllerAvailability(controller);
  const locked = !!step.needsController && !availability.runnable;
  const go = (i: number) => setSearch({ step: String(i + 1) });

  return (
    <div className="space-y-6">
      <PageHeader title="Guided demo" description="Seven scenarios that show what this system guarantees, in about fifteen minutes. Nothing here runs by itself." />
      <ol className="flex flex-wrap gap-1.5" aria-label="Steps">
        {STEPS.map((s, i) => (
          <li key={s.title}>
            <button
              type="button"
              onClick={() => go(i)}
              aria-current={i === index ? "step" : undefined}
              className={cn(
                "rounded-full border px-2.5 py-1 text-xs outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50",
                i === index ? "border-primary bg-primary text-primary-foreground" : "hover:bg-accent/50",
              )}
            >
              {i + 1}. {s.title}
            </button>
          </li>
        ))}
      </ol>
      <Card>
        <CardContent className="space-y-4 p-5" data-testid="demo-step">
          <div className="flex items-start gap-3">
            <Compass className="mt-0.5 size-5 text-muted-foreground" aria-hidden />
            <div>
              <div className="text-xs text-muted-foreground">
                Step {index + 1} of {STEPS.length}
              </div>
              <h2 className="text-lg font-semibold">{step.title}</h2>
              <p className="text-sm text-muted-foreground">{step.why}</p>
            </div>
          </div>
          <dl className="grid gap-3 text-sm sm:grid-cols-2">
            <div>
              <dt className="text-xs font-medium text-muted-foreground">Safe defaults</dt>
              <dd>{step.defaults}</dd>
            </div>
            <div>
              <dt className="text-xs font-medium text-muted-foreground">Expected behaviour</dt>
              <dd>{step.expected}</dd>
            </div>
          </dl>
          <Observed step={step} runs={runs.data} controllerOk={availability.runnable} />
          <div className="flex flex-wrap items-center gap-2">
            {locked ? (
              <span className="inline-flex items-center gap-1.5 text-sm text-muted-foreground" data-testid="demo-locked">
                <Lock className="size-4" /> {availability.reason}
              </span>
            ) : (
              <Button asChild>
                <Link to={step.to}>
                  <ExternalLink /> {step.linkLabel}
                </Link>
              </Button>
            )}
            <span className="flex-1" />
            <Button variant="ghost" disabled={index === 0} onClick={() => go(index - 1)}>
              <ArrowLeft /> Back
            </Button>
            <Button variant="outline" disabled={index === STEPS.length - 1} onClick={() => go(index + 1)}>
              Next <ArrowRight />
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
