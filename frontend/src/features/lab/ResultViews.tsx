/**
 * Experiment-specific result panels. They read `run.result.data`, whose shape is defined
 * per experiment in app/lab/experiments/*.py; every field is read defensively because a
 * failed or partial run may lack some of them.
 */
import { IdText, KeyValue, Money } from "@/components/common/display";
import { ReplicaDistribution } from "@/components/lab/flow/ReplicaDistribution";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";
import type { LabRun } from "@/types/api";
import { FailureResultView, FailureVisual } from "./FailureViews";

type Data = Record<string, unknown>;

function obj(value: unknown): Data {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Data) : {};
}
function num(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}
function str(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

export function ExperimentResultView({ run }: { run: LabRun }) {
  const data = obj(run.result?.data);
  switch (run.experiment) {
    case "concurrency":
      return <ConcurrencyView data={data} />;
    case "idempotency":
      return <IdempotencyView data={data} />;
    case "rate_limiting":
      return <RateLimitView data={data} />;
    case "load_balancing":
      return <LoadBalancingView data={data} />;
    default:
      return <FailureResultView run={run} />;
  }
}

function ByReplicaTable({ byReplica, columns }: { byReplica: Data; columns: { key: string; label: string; tone?: string }[] }) {
  const replicas = Object.keys(byReplica).sort();
  if (!replicas.length) return null;
  return (
    <Table aria-label="Outcomes per replica">
      <TableHeader>
        <TableRow>
          <TableHead>Replica</TableHead>
          {columns.map((c) => (
            <TableHead key={c.key} className="text-right">
              {c.label}
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {replicas.map((replica) => {
          const row = obj(byReplica[replica]);
          return (
            <TableRow key={replica}>
              <TableCell className="font-mono text-xs">{replica}</TableCell>
              {columns.map((c) => (
                <TableCell key={c.key} className={cn("text-right font-mono tabular", c.tone)}>
                  {num(row[c.key]) ?? 0}
                </TableCell>
              ))}
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
  );
}

function ConcurrencyView({ data }: { data: Data }) {
  const balances = obj(data.final_balances);
  return (
    <div className="space-y-4" data-testid="result-concurrency">
      <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <KeyValue label="Expected successes">
          <span className="font-mono">{num(data.expected_successes) ?? "—"}</span>
        </KeyValue>
        <KeyValue label="Source balance">{num(balances.source) !== null ? <Money minor={num(balances.source)!} /> : "—"}</KeyValue>
        <KeyValue label="Destination balance">{num(balances.destination) !== null ? <Money minor={num(balances.destination)!} /> : "—"}</KeyValue>
        <KeyValue label="Source account">{str(data.source_account_id) ? <IdText id={str(data.source_account_id)!} /> : "—"}</KeyValue>
      </dl>
      <p className="text-xs text-muted-foreground">Final outcome of each logical transfer, by the replica that answered it last:</p>
      <ByReplicaTable
        byReplica={obj(data.by_replica)}
        columns={[
          { key: "committed", label: "Committed", tone: "text-success" },
          { key: "insufficient_funds", label: "Insufficient funds", tone: "text-warning" },
        ]}
      />
    </div>
  );
}

function IdempotencyView({ data }: { data: Data }) {
  const ids = Array.isArray(data.transfer_ids) ? (data.transfer_ids as string[]) : [];
  const reuse = obj(data.reuse_probe);
  const after = obj(data.balances_after_duplicates);
  return (
    <dl className="grid grid-cols-1 gap-4 sm:grid-cols-2" data-testid="result-idempotency">
      <KeyValue label="Idempotency-Key">
        <code className="font-mono text-xs">{str(data.idempotency_key) ?? "—"}</code>
      </KeyValue>
      <KeyValue label="Transfer every duplicate received">{ids[0] ? <IdText id={ids[0]} /> : "—"}</KeyValue>
      <KeyValue label="Balances after the duplicates">
        {num(after.source) !== null ? (
          <span>
            source <Money minor={num(after.source)!} /> · destination <Money minor={num(after.destination) ?? 0} />
          </span>
        ) : (
          "—"
        )}
      </KeyValue>
      <KeyValue label="Same key, different amount">
        <code className="font-mono text-xs">
          {num(reuse.status) ?? "—"} {str(reuse.code) ?? ""}
        </code>
      </KeyValue>
      <KeyValue label="Fresh key">{str(data.fresh_transfer_id) ? <IdText id={str(data.fresh_transfer_id)!} /> : "—"}</KeyValue>
    </dl>
  );
}

function RateLimitView({ data }: { data: Data }) {
  const policy = obj(data.policy);
  const burst = obj(data.burst);
  const probe = obj(data.probe);
  const sequence = Array.isArray(burst.admitted_sequence) ? (burst.admitted_sequence as boolean[]) : [];
  const capacity = num(policy.capacity) ?? 0;
  const allowed = num(burst.allowed) ?? 0;
  const counterfactual = num(data.counterfactual_per_replica);
  const compareMax = Math.max(allowed, counterfactual ?? 0, 1);
  return (
    <div className="space-y-5" data-testid="result-rate-limit">
      <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <KeyValue label="Policy">
          <code className="font-mono text-xs">
            {str(policy.name) ?? "write"}: {capacity} burst, {num(policy.tokens_per_second) ?? "?"}/s
          </code>
        </KeyValue>
        <KeyValue label="Limiter">
          <code className="font-mono text-xs">{str(data.limiter_backend) ?? "—"}</code>
        </KeyValue>
        <KeyValue label="Burst duration">
          <span className="font-mono">{num(burst.elapsed_ms) ?? "—"} ms</span>
        </KeyValue>
        <KeyValue label="Retry-After seen">
          <span className="font-mono">{Array.isArray(data.retry_after_values) ? (data.retry_after_values as number[]).join(", ") || "—" : "—"} s</span>
        </KeyValue>
      </dl>

      <div>
        <div className="mb-1.5 flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
          <span>Burst requests in order: admitted (token) or rejected (429)</span>
          <span className="flex items-center gap-3">
            <span className="flex items-center gap-1">
              <span className="size-2.5 rounded-sm bg-success" /> admitted {allowed}
            </span>
            <span className="flex items-center gap-1">
              <span className="size-2.5 rounded-sm bg-warning" /> 429 {num(burst.rejected) ?? 0}
            </span>
          </span>
        </div>
        <ol className="flex flex-wrap gap-1" aria-label="Burst requests: admitted or rejected">
          {sequence.map((ok, i) => (
            <li
              key={i}
              aria-label={`Request ${i + 1}: ${ok ? "admitted" : "rejected 429"}`}
              title={`#${i + 1}: ${ok ? "admitted" : "429"}`}
              className={cn("size-4 rounded-[3px]", ok ? "bg-success" : "bg-warning/70")}
            />
          ))}
        </ol>
        <p className="mt-1.5 text-xs text-muted-foreground">
          The order is the order requests were released, not the order they reached Redis, so admitted and rejected requests interleave.
        </p>
      </div>

      {counterfactual !== null && (
        <div className="space-y-2" aria-label="Global quota versus per-replica counters">
          <div className="text-xs font-medium text-muted-foreground">Same traffic, two designs</div>
          {[
            { label: "Shared Redis bucket (actual)", value: allowed, tone: "bg-success" },
            { label: "Per-replica counters (counterfactual)", value: counterfactual, tone: "bg-destructive/70" },
          ].map((row) => (
            <div key={row.label} className="grid grid-cols-[minmax(0,14rem)_1fr_3rem] items-center gap-3 text-xs">
              <span className="truncate">{row.label}</span>
              <div className="h-3 overflow-hidden rounded-full bg-muted">
                <div className={cn("h-full rounded-full", row.tone)} style={{ width: `${(row.value / compareMax) * 100}%` }} />
              </div>
              <span className="text-right font-mono tabular">{row.value}</span>
            </div>
          ))}
        </div>
      )}

      <ByReplicaTable
        byReplica={obj(burst.by_replica)}
        columns={[
          { key: "admitted", label: "Admitted", tone: "text-success" },
          { key: "rejected", label: "Rejected (429)", tone: "text-warning" },
        ]}
      />

      {num(probe.requests) ? (
        <div className="rounded-lg border bg-muted/20 px-4 py-3 text-sm">
          <span className="font-medium">Refill: </span>
          after waiting {num(probe.wait_seconds)} s, <span className="font-mono">{num(probe.allowed)}</span> of {num(probe.requests)} requests were admitted (expected ≈{" "}
          {num(probe.expected)}; accepted range {Array.isArray(probe.allowed_range) ? (probe.allowed_range as number[]).join("–") : "—"}, which includes tokens that
          refilled while the second burst was in flight).
        </div>
      ) : null}
    </div>
  );
}

function LoadBalancingView({ data }: { data: Data }) {
  const sequence = Array.isArray(data.sequence) ? (data.sequence as string[]) : [];
  const counts = obj(data.counts) as Record<string, number>;
  const latency = obj(data.latency_by_replica_ms);
  return (
    <div className="space-y-4" data-testid="result-load-balancing">
      <ReplicaDistribution sequence={sequence} counts={counts} />
      <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <KeyValue label="Fair share">
          <span className="font-mono">{num(data.fair_share) ?? "—"}</span>
        </KeyValue>
        <KeyValue label="Spread (max − min)">
          <span className="font-mono">
            {num(data.spread) ?? "—"} <span className="text-muted-foreground">(tolerance {num(data.tolerance) ?? "—"})</span>
          </span>
        </KeyValue>
        <KeyValue label="Replica changes">
          <span className="font-mono">
            {num(data.alternations) ?? "—"} / {Math.max(0, sequence.length - 1)}
          </span>
        </KeyValue>
        <KeyValue label="Median latency by replica">
          <span className="font-mono text-xs">
            {Object.keys(latency)
              .sort()
              .map((name) => `${name} ${num(obj(latency[name]).p50) ?? "?"} ms`)
              .join(" · ") || "—"}
          </span>
        </KeyValue>
      </dl>
    </div>
  );
}

// --- compact visual for the result summary ------------------------------------------------

function StackedBar({ parts, label }: { parts: { label: string; value: number; tone: string }[]; label: string }) {
  const total = parts.reduce((s, p) => s + p.value, 0) || 1;
  const visible = parts.filter((p) => p.value > 0);
  return (
    <div className="space-y-1.5">
      <div className="flex h-2.5 overflow-hidden rounded-full bg-muted" role="img" aria-label={`${label}: ${visible.map((p) => `${p.value} ${p.label}`).join(", ")}`}>
        {visible.map((p) => (
          <div key={p.label} className={p.tone} style={{ width: `${(p.value / total) * 100}%` }} />
        ))}
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        {visible.map((p) => (
          <span key={p.label} className="flex items-center gap-1.5">
            <span className={cn("size-2 rounded-sm", p.tone)} aria-hidden />
            {p.value} {p.label}
          </span>
        ))}
      </div>
    </div>
  );
}

/** One small picture of the outcome, shown in the result summary. Details live in the Details tab. */
export function ResultVisual({ run }: { run: LabRun }) {
  const data = obj(run.result?.data);
  const counts = run.result?.outcome_counts ?? {};
  switch (run.experiment) {
    case "concurrency": {
      const ok = counts.committed ?? 0;
      const rejected = counts.insufficient_funds ?? 0;
      const other = Object.entries(counts).reduce((s, [k, v]) => (k === "committed" || k === "insufficient_funds" ? s : s + v), 0);
      return (
        <StackedBar
          label="Final outcomes"
          parts={[
            { label: "succeeded", value: ok, tone: "bg-success" },
            { label: "rejected: insufficient funds", value: rejected, tone: "bg-warning/80" },
            { label: "unexpected", value: other, tone: "bg-destructive" },
          ]}
        />
      );
    }
    case "idempotency":
      return (
        <StackedBar
          label="Duplicate responses"
          parts={[
            { label: "executed", value: counts.committed ?? 0, tone: "bg-success" },
            { label: "replayed", value: counts.replayed ?? 0, tone: "bg-info" },
            {
              label: "other",
              value: Object.entries(counts).reduce((s, [k, v]) => (k === "committed" || k === "replayed" ? s : s + v), 0),
              tone: "bg-destructive",
            },
          ]}
        />
      );
    case "rate_limiting": {
      const burst = obj(data.burst);
      return (
        <StackedBar
          label="Burst"
          parts={[
            { label: "admitted", value: num(burst.allowed) ?? 0, tone: "bg-success" },
            { label: "rejected (429)", value: num(burst.rejected) ?? 0, tone: "bg-warning/80" },
          ]}
        />
      );
    }
    case "load_balancing":
      return (
        <ReplicaDistribution
          sequence={Array.isArray(data.sequence) ? (data.sequence as string[]) : []}
          counts={obj(data.counts) as Record<string, number>}
          showSequence={false}
        />
      );
    default:
      return <FailureVisual run={run} />;
  }
}
