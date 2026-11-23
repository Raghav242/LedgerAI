/**
 * Exportable experiment report (JSON). Two clearly separated sections:
 *   observed     - what the run measured and checked (from the persisted run and its timeline)
 *   explanatory  - teaching text from the catalog and the result (not observations)
 * Pure function: the same run always yields the same report, apart from `exported_at`.
 */
import type { ExperimentDefinition, LabEvent, LabRun } from "@/types/api";

export const REPORT_VERSION = 1;

function idsFrom(data: unknown): Record<string, string> {
  const out: Record<string, string> = {};
  if (typeof data !== "object" || data === null) return out;
  for (const [key, value] of Object.entries(data)) {
    if (typeof value === "string" && /(^|_)id$/.test(key)) out[key] = value;
  }
  return out;
}

export function buildReport(run: LabRun, events: LabEvent[], definition: ExperimentDefinition | undefined, exportedAt = new Date()) {
  const requests = events.filter((e) => e.request).map((e) => e.request!);
  const replicas = [...new Set(requests.map((r) => r.replica).filter((r): r is string => !!r))].sort();
  const requestIds = [...new Set(requests.map((r) => r.request_id).filter((r): r is string => !!r))];
  return {
    report_version: REPORT_VERSION,
    exported_at: exportedAt.toISOString(),
    experiment: { id: run.experiment, title: definition?.title ?? run.experiment },
    run_id: run.id,
    outcome: run.status,
    configuration: run.params,
    timestamps: { created_at: run.created_at, started_at: run.started_at, finished_at: run.finished_at },
    environment: {
      runner: run.runner_replica,
      target: run.target_base_url,
      replicas_observed: replicas,
      replica_count_observed: replicas.length,
    },
    observed: {
      summary: run.result?.summary ?? null,
      error: run.error,
      metrics: run.result?.metrics ?? [],
      outcome_counts: run.result?.outcome_counts ?? {},
      invariant_checks: run.checks ?? [],
      data: run.result?.data ?? {},
      important_ids: { ...idsFrom(run.result?.data), request_ids_sample: requestIds.slice(0, 20) },
      timeline: { events_recorded: run.event_count, events_dropped: run.events_dropped, requests_in_export: requests.length },
    },
    explanatory: {
      note: "Teaching text from the experiment catalog and result. Not an observation.",
      concept: definition?.concept ?? null,
      expected: definition?.expected ?? [],
      explanation: run.result?.explanation ?? [],
    },
  };
}

export function downloadReport(report: ReturnType<typeof buildReport>) {
  const blob = new Blob([JSON.stringify(report, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `ledgerflow-${report.experiment.id}-${report.run_id.slice(0, 8)}.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
