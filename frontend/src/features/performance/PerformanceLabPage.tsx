import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Gauge, Loader2, Play, ShieldAlert } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Bar, BarChart, CartesianGrid, LabelList, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { isApiError, API_V1, http } from "@/api/client";
import { Disclosure } from "@/components/common/Disclosure";
import { KeyValue, PageHeader } from "@/components/common/display";
import { StatusSummary } from "@/components/common/StatusSummary";
import { EmptyState, ErrorState, LoadingRows } from "@/components/common/states";
import { Tabs } from "@/components/common/Tabs";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useControllerStatus } from "@/features/lab/hooks";
import type { components } from "@/types/openapi.gen";
import { WORKLOAD_LABEL, insights, scalingRows, withSpread, workloads, type BenchmarkRun } from "./insights";

type BenchmarkList = components["schemas"]["BenchmarkList"];

interface BenchmarkStatus {
  state: "idle" | "running" | "succeeded" | "failed";
  started_at: string | null;
  finished_at: string | null;
  exit_code: number | null;
  result_file: string | null;
  log_tail: string[];
}

const SERIES = [
  { workload: "ReadHeavyUser", color: "var(--chart-1)" },
  { workload: "MixedTransferUser", color: "var(--chart-3)" },
  { workload: "HotAccountUser", color: "var(--chart-2)" },
];

function describeRun(run: BenchmarkRun): string {
  const m = run.meta as Record<string, unknown>;
  const first = run.results[0];
  const parts = [
    run.started_at ? new Date(run.started_at).toLocaleString() : run.id,
    `${String(m.duration_s ?? first?.duration_s ?? "?")} s per run`,
    `${String(m.users ?? first?.users ?? "?")} users`,
    `Locust ${String(m.locust ?? "?")}`,
    String(m.host ?? "host unknown"),
  ];
  return parts.join(" · ");
}

function ThroughputChart({ run }: { run: BenchmarkRun }) {
  const replicaCounts = [...new Set(run.results.filter((r) => r.label.startsWith("scale-")).map((r) => r.replicas))].sort((a, b) => a - b);
  const data = replicaCounts.map((n) => {
    const row: Record<string, number | string> = { replicas: `${n} replica${n === 1 ? "" : "s"}` };
    for (const s of SERIES) {
      const r = run.results.find((x) => x.label.startsWith("scale-") && x.replicas === n && x.workload === s.workload);
      if (r) row[s.workload] = Math.round(r.rps);
    }
    return row;
  });
  const present = SERIES.filter((s) => data.some((d) => typeof d[s.workload] === "number"));
  return (
    <div
      className="h-72 w-full"
      role="img"
      aria-label={`Throughput in requests per second by replica count: ${data
        .map((d) => `${d.replicas}: ${present.map((s) => `${WORKLOAD_LABEL[s.workload]} ${d[s.workload] ?? "n/a"}`).join(", ")}`)
        .join("; ")}`}
    >
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 20, right: 8, bottom: 0, left: -8 }} barGap={2}>
          <CartesianGrid vertical={false} stroke="var(--border)" />
          <XAxis dataKey="replicas" tickLine={false} axisLine={false} tick={{ fill: "var(--muted-foreground)", fontSize: 12 }} />
          <YAxis tickLine={false} axisLine={false} tick={{ fill: "var(--muted-foreground)", fontSize: 11 }} width={48} />
          <Tooltip
            cursor={{ fill: "var(--muted)", opacity: 0.5 }}
            contentStyle={{ background: "var(--popover)", border: "1px solid var(--border)", borderRadius: 8, fontSize: 12 }}
            labelStyle={{ color: "var(--foreground)" }}
            itemStyle={{ color: "var(--foreground)" }}
            formatter={(value, name) => [`${String(value)} req/s`, WORKLOAD_LABEL[String(name)] ?? String(name)]}
          />
          <Legend formatter={(value) => <span style={{ color: "var(--foreground)", fontSize: 12 }}>{WORKLOAD_LABEL[String(value)] ?? String(value)}</span>} />
          {present.map((s) => (
            <Bar key={s.workload} dataKey={s.workload} fill={s.color} radius={[4, 4, 0, 0]} maxBarSize={44} isAnimationActive={false}>
              <LabelList dataKey={s.workload} position="top" style={{ fill: "var(--muted-foreground)", fontSize: 11 }} />
            </Bar>
          ))}
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function MetricsTable({ run, workload }: { run: BenchmarkRun; workload: string }) {
  const rows = scalingRows(run, workload);
  return (
    <div className="overflow-x-auto rounded-lg border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Replicas</TableHead>
            <TableHead className="text-right">Throughput</TableHead>
            <TableHead className="text-right">p50</TableHead>
            <TableHead className="text-right">p95</TableHead>
            <TableHead className="text-right">p99</TableHead>
            <TableHead className="text-right">Errors</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((r) => (
            <TableRow key={r.label}>
              <TableCell>{r.replicas}</TableCell>
              <TableCell className="text-right tabular-nums">{withSpread(r.rps, r.rps_std)} req/s</TableCell>
              <TableCell className="text-right tabular-nums">{Math.round(r.p50_ms)} ms</TableCell>
              <TableCell className="text-right tabular-nums">{withSpread(r.p95_ms, r.p95_std, "")} ms</TableCell>
              <TableCell className="text-right tabular-nums">{Math.round(r.p99_ms)} ms</TableCell>
              <TableCell className="text-right tabular-nums">{r.error_pct.toFixed(2)}%</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function RawResults({ run }: { run: BenchmarkRun }) {
  return (
    <div className="space-y-3">
      <div className="overflow-x-auto rounded-lg border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Run</TableHead>
              <TableHead className="text-right">Requests</TableHead>
              <TableHead className="text-right">429s</TableHead>
              <TableHead className="text-right">Other failures</TableHead>
              <TableHead className="text-right">PG conns max / mean</TableHead>
              <TableHead>CPU % mid-run</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {run.results.map((r) => (
              <TableRow key={r.label}>
                <TableCell className="whitespace-nowrap">{r.label}</TableCell>
                <TableCell className="text-right tabular-nums">{r.requests}</TableCell>
                <TableCell className="text-right tabular-nums">{r.rejected_429}</TableCell>
                <TableCell className="text-right tabular-nums">{r.other_failures}</TableCell>
                <TableCell className="text-right tabular-nums">
                  {r.pg_connections_max ?? "—"} / {r.pg_connections_mean ?? "—"}
                </TableCell>
                <TableCell className="text-xs text-muted-foreground">
                  {Object.entries(r.cpu_percent ?? {})
                    .map(([k, v]) => `${k} ${Math.round(v)}`)
                    .join(", ") || "—"}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
      <pre className="overflow-x-auto rounded-lg border bg-muted/30 p-3 font-mono text-xs">{JSON.stringify(run.meta, null, 2)}</pre>
    </div>
  );
}

function NewBenchmark({ onFinished }: { onFinished: () => void }) {
  const controller = useControllerStatus(true);
  const queryClient = useQueryClient();
  const [form, setForm] = useState({ duration_s: "20", users: "32", r1: true, r2: true, r4: false, quota: false });
  const [confirming, setConfirming] = useState(false);
  const status = useQuery({
    queryKey: ["controller", "benchmark"],
    queryFn: async ({ signal }) => (await http.get<BenchmarkStatus>("/controller/benchmarks/current", { signal })).data,
    enabled: !!controller.data?.benchmarks_enabled,
    refetchInterval: (q) => (q.state.data?.state === "running" ? 2_000 : 10_000),
  });
  const start = useMutation({
    mutationFn: async () =>
      (
        await http.post<BenchmarkStatus>("/controller/benchmarks", {
          duration_s: Number(form.duration_s),
          users: Number(form.users),
          replicas: [form.r1 && 1, form.r2 && 2, form.r4 && 4].filter((x): x is number => typeof x === "number"),
          include_quota_run: form.quota,
          confirm: true,
        })
      ).data,
    onSuccess: (s) => queryClient.setQueryData(["controller", "benchmark"], s),
  });
  // When a run finishes, its result file exists: refresh the recorded runs (once per run).
  const finishedAt = status.data?.state === "succeeded" ? status.data.finished_at : null;
  const seen = useRef<string | null>(null);
  useEffect(() => {
    if (finishedAt && finishedAt !== seen.current) {
      if (seen.current !== null || status.data?.state === "succeeded") onFinished();
      seen.current = finishedAt;
    }
  }, [finishedAt, onFinished, status.data?.state]);

  if (controller.isLoading) return <LoadingRows rows={2} />;
  if (controller.error || !controller.data)
    return <p className="text-sm text-muted-foreground">New runs need the local experiment controller: python -m labctl --enable-benchmarks</p>;
  if (!controller.data.benchmarks_enabled)
    return <p className="text-sm text-muted-foreground">The controller is running without benchmarks. Restart it with --enable-benchmarks.</p>;

  const running = status.data?.state === "running";
  const replicaCount = [form.r1, form.r2, form.r4].filter(Boolean).length;
  return (
    <div className="space-y-4">
      {running && (
        <StatusSummary tone="info" title="Benchmark running" data-testid="benchmark-running">
          While it runs, the stack is redeployed against the separate benchmark database; other pages will show that database until it finishes and the
          default stack is restored.
        </StatusSummary>
      )}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <label className="space-y-1 text-sm">
          <span className="text-xs text-muted-foreground">Seconds per run (10–60)</span>
          <Input inputMode="numeric" value={form.duration_s} onChange={(e) => setForm({ ...form, duration_s: e.target.value })} />
        </label>
        <label className="space-y-1 text-sm">
          <span className="text-xs text-muted-foreground">Users (8–64)</span>
          <Input inputMode="numeric" value={form.users} onChange={(e) => setForm({ ...form, users: e.target.value })} />
        </label>
        <fieldset className="col-span-2 space-y-1 text-sm">
          <legend className="text-xs text-muted-foreground">Replica counts</legend>
          <div className="flex flex-wrap gap-3 pt-1.5">
            {(["r1", "r2", "r4"] as const).map((k) => (
              <label key={k} className="flex items-center gap-1.5">
                <input type="checkbox" checked={form[k]} onChange={(e) => setForm({ ...form, [k]: e.target.checked })} /> {k.slice(1)}
              </label>
            ))}
            <label className="flex items-center gap-1.5">
              <input type="checkbox" checked={form.quota} onChange={(e) => setForm({ ...form, quota: e.target.checked })} /> small-quota run
            </label>
          </div>
        </fieldset>
      </div>
      <Button disabled={running || start.isPending || replicaCount === 0} onClick={() => setConfirming(true)}>
        {running || start.isPending ? <Loader2 className="animate-spin" /> : <Play />} Run benchmark
      </Button>
      {start.error && <ErrorState error={start.error} compact />}
      {status.data && status.data.state !== "idle" && (
        <Disclosure title="Harness output" meta={status.data.state} defaultOpen={running}>
          <pre className="max-h-64 overflow-auto rounded-lg border bg-muted/30 p-3 font-mono text-[11px]">{status.data.log_tail.join("\n") || "…"}</pre>
        </Disclosure>
      )}
      <Dialog open={confirming} onOpenChange={setConfirming}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <ShieldAlert className="size-4 text-warning" /> Run an isolated benchmark stack?
            </DialogTitle>
            <DialogDescription>
              The harness starts a separate, isolated benchmark stack (Compose project ledgerflow-bench with its own database), runs Locust for {form.duration_s} s per
              configuration ({replicaCount * 2 + (form.quota ? 1 : 0)} runs), then removes it. Expect several minutes; no other experiment
              can run meanwhile. Your development stack and data are not touched.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setConfirming(false)}>
              Cancel
            </Button>
            <Button
              onClick={() => {
                setConfirming(false);
                start.mutate();
              }}
            >
              <Play /> Start
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

/** Recorded benchmark results: the comparison first, details and new runs on demand. */
export function PerformanceLabPage() {
  // After a run finishes, keep re-reading for a while: the result file can take a moment to
  // appear in the replicas' bind-mounted directory (observed on Docker Desktop).
  const [awaitNewerThan, setAwaitNewerThan] = useState<{ newest: string | null; until: number } | null>(null);
  const benchmarks = useQuery({
    queryKey: ["performance", "benchmarks"],
    queryFn: async ({ signal }) => (await http.get<BenchmarkList>(`${API_V1}/performance/benchmarks`, { signal })).data,
    refetchInterval: (q) => {
      if (!awaitNewerThan || Date.now() > awaitNewerThan.until) return false;
      const newest = q.state.data?.runs[0]?.id ?? null;
      return newest !== awaitNewerThan.newest ? false : 3_000;
    },
  });
  const usable = (benchmarks.data?.runs ?? []).filter((r) => !r.error && workloads(r).length > 0);
  const [runId, setRunId] = useState<string | null>(null);
  const run = usable.find((r) => r.id === runId) ?? usable[0];
  const [workload, setWorkload] = useState("MixedTransferUser");
  const disabled = isApiError(benchmarks.error) && benchmarks.error.status === 404;
  const queryClient = useQueryClient();
  const newestId = benchmarks.data?.runs[0]?.id ?? null;
  const refreshRuns = useCallback(() => {
    setRunId(null); // show the newest run
    setAwaitNewerThan({ newest: newestId, until: Date.now() + 90_000 });
    void queryClient.invalidateQueries({ queryKey: ["performance", "benchmarks"] });
  }, [queryClient, newestId]);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Performance Lab"
        description="Throughput and latency of the real stack with 1, 2 and 4 API replicas, from recorded Locust runs."
        actions={<Badge variant="outline">Recorded results, not live</Badge>}
      />
      {benchmarks.isLoading ? (
        <LoadingRows rows={4} />
      ) : disabled ? (
        <Card>
          <CardContent>
            <EmptyState icon={<Gauge className="size-5" />} title="The Performance Lab is disabled on this backend" description="It is part of the development-only surface (ENABLE_DEV_ENDPOINTS=true)." />
          </CardContent>
        </Card>
      ) : benchmarks.error ? (
        <Card>
          <CardContent>
            <ErrorState error={benchmarks.error} onRetry={() => void benchmarks.refetch()} />
          </CardContent>
        </Card>
      ) : !run ? (
        <Card>
          <CardContent>
            <EmptyState
              icon={<Gauge className="size-5" />}
              title={benchmarks.data?.directory_found ? "No benchmark results recorded yet" : "Benchmark results directory not found"}
              description="Run python benchmarks/run_benchmarks.py, or start a run below with the local controller."
            />
          </CardContent>
        </Card>
      ) : (
        <>
          <Card>
            <CardContent className="space-y-3 p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 className="font-semibold">Throughput by replica count</h2>
                {usable.length > 1 && (
                  <div className="w-full sm:w-64">
                    <NativeSelect aria-label="Benchmark run" value={run.id} onChange={(e) => setRunId(e.target.value)}>
                      {usable.map((r) => (
                        <option key={r.id} value={r.id}>
                          {r.started_at ? new Date(r.started_at).toLocaleString() : r.id}
                        </option>
                      ))}
                    </NativeSelect>
                  </div>
                )}
              </div>
              <p className="text-xs text-muted-foreground" data-testid="benchmark-source">
                {describeRun(run)}
              </p>
              <ThroughputChart run={run} />
            </CardContent>
          </Card>

          <Card className="gap-0 py-0">
            <Tabs<string>
              idPrefix="workload"
              label="Workload"
              value={workloads(run).includes(workload) ? workload : workloads(run)[0]!}
              onChange={setWorkload}
              className="px-2 pt-1"
              items={workloads(run).map((w) => ({ id: w, label: WORKLOAD_LABEL[w] ?? w }))}
            />
            <CardContent className="p-4">
              <MetricsTable run={run} workload={workloads(run).includes(workload) ? workload : workloads(run)[0]!} />
            </CardContent>
          </Card>

          <Card>
            <CardContent className="space-y-2 p-4">
              <h2 className="font-semibold">What did we learn?</h2>
              <ul className="space-y-1.5 text-sm" data-testid="insights">
                {insights(run)
                  .filter((i) => !i.detail)
                  .map((i) => (
                    <li key={i.text} className={i.tone === "warning" ? "text-warning" : undefined}>
                      {i.text}
                    </li>
                  ))}
              </ul>
              {insights(run).some((i) => i.detail) && (
                <Disclosure title="Latency, connections and run-to-run spread" meta={String(insights(run).filter((i) => i.detail).length)}>
                  <ul className="space-y-1.5 text-sm text-muted-foreground" data-testid="insight-details">
                    {insights(run)
                      .filter((i) => i.detail)
                      .map((i) => (
                        <li key={i.text}>{i.text}</li>
                      ))}
                  </ul>
                </Disclosure>
              )}
              <p className="text-xs text-muted-foreground">
                Derived only from the numbers above. Everything ran on one machine, so the replicas, PostgreSQL, Redis, NGINX and the load generator
                share its CPUs: more replicas cannot scale linearly here.
              </p>
            </CardContent>
          </Card>

          <Disclosure variant="boxed" className="bg-card" title="Raw configuration and statistics" meta={run.id}>
            <dl className="mb-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
              <KeyValue label="Started">{run.started_at ? new Date(run.started_at).toLocaleString() : "—"}</KeyValue>
              <KeyValue label="Runs">{run.results.length}</KeyValue>
              <KeyValue label="Source">{String((run.meta as Record<string, unknown>).source ?? "cli")}</KeyValue>
              <KeyValue label="Database">{String((run.meta as Record<string, unknown>).database ?? "ledgerflow_bench")}</KeyValue>
            </dl>
            <RawResults run={run} />
          </Disclosure>
        </>
      )}
      <Disclosure variant="boxed" className="bg-card" title="Run a new benchmark (local controller)">
        <NewBenchmark onFinished={refreshRuns} />
      </Disclosure>
    </div>
  );
}
