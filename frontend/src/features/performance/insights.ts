/**
 * "What did we learn?" — statements DERIVED from recorded benchmark numbers only.
 * Nothing here is measured or estimated; if a number is missing, no statement is made.
 */
import type { components } from "@/types/openapi.gen";

export type BenchmarkResult = components["schemas"]["BenchmarkResult"];
export type BenchmarkRun = components["schemas"]["BenchmarkRun"];

export const WORKLOAD_LABEL: Record<string, string> = {
  ReadHeavyUser: "Read-heavy",
  MixedTransferUser: "Mixed transfers",
};

export function scalingRows(run: BenchmarkRun, workload: string): BenchmarkResult[] {
  return run.results.filter((r) => r.workload === workload && r.label.startsWith("scale-")).sort((a, b) => a.replicas - b.replicas);
}

export function workloads(run: BenchmarkRun): string[] {
  return [...new Set(run.results.filter((r) => r.label.startsWith("scale-")).map((r) => r.workload))];
}

export interface Insight {
  tone: "info" | "warning";
  text: string;
}

export function insights(run: BenchmarkRun): Insight[] {
  const out: Insight[] = [];
  for (const workload of workloads(run)) {
    const rows = scalingRows(run, workload);
    const base = rows.find((r) => r.replicas === 1);
    const name = WORKLOAD_LABEL[workload] ?? workload;
    if (!base || rows.length < 2) continue;
    const steps = rows
      .filter((r) => r.replicas > 1)
      .map((r) => {
        const speedup = r.rps / base.rps;
        return { r, speedup, efficiency: speedup / r.replicas };
      });
    out.push({
      tone: "info",
      text: `${name}: ${steps
        .map((s) => `${s.r.replicas} replicas gave ${s.speedup.toFixed(2)}x the throughput of one (${Math.round(s.efficiency * 100)}% of linear)`)
        .join("; ")}.`,
    });
    const last = steps.at(-1);
    if (last && last.efficiency < 0.85) {
      out.push({ tone: "warning", text: `${name}: scaling is sub-linear: each added replica contributed less than the first.` });
    }
    const top = rows.at(-1)!;
    out.push({
      tone: "info",
      text: `${name}: p95 latency went from ${Math.round(base.p95_ms)} ms with 1 replica to ${Math.round(top.p95_ms)} ms with ${top.replicas}; p99 from ${Math.round(base.p99_ms)} to ${Math.round(top.p99_ms)} ms.`,
    });
    const cpu = top.cpu_percent ?? {};
    const apiCpu = Object.entries(cpu).filter(([k]) => k.startsWith("api-")).map(([, v]) => v);
    if (apiCpu.length && Math.min(...apiCpu) >= 90) {
      out.push({
        tone: "info",
        text: `${name}: at ${top.replicas} replicas every API container used ≥ ${Math.round(Math.min(...apiCpu))}% CPU (100% = one core) mid-run, while PostgreSQL used ${Math.round(cpu.postgres ?? 0)}%: the replicas' CPU, on one shared machine, is the measured limit.`,
      });
    }
    if (top.pg_connections_max) {
      out.push({ tone: "info", text: `${name}: up to ${top.pg_connections_max} PostgreSQL connections were open at ${top.replicas} replicas (each replica pools at most 10).` });
    }
  }
  const quota = run.results.find((r) => r.label === "small-quota");
  if (quota) {
    out.push({ tone: "info", text: `Small write quota: ${quota.rejected_429} requests were rejected with 429; ${quota.notes.find((n) => n.startsWith("admitted")) ?? ""}` });
  }
  return out;
}
