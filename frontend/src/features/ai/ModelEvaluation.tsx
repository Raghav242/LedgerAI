import { useQuery } from "@tanstack/react-query";
import { aiApi, type ModelInfo } from "@/api/ai";
import { Disclosure } from "@/components/common/Disclosure";
import { KeyValue } from "@/components/common/display";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";

/** Shape of app/ai/model/evaluation.json (written by python -m app.ai.evaluate). */
interface Metrics {
  precision: number;
  recall: number;
  f1: number;
  false_positive_rate: number;
  recall_by_kind: Record<string, number>;
}
interface Dataset {
  rows: number;
  anomalies: Record<string, number>;
  detectors: Record<string, Metrics>;
  scenario?: string;
}
interface Spread {
  mean: number;
  std: number;
}
interface Report {
  generated_at: string;
  detectors: Record<string, string>;
  datasets: Record<string, Dataset>;
  fresh_seeds_summary: Record<string, Record<string, Spread> & { recall_by_kind: Record<string, Spread> }>;
  disagreements_held_out: Record<"model_only" | "rules_only" | "both", Record<string, number>>;
  inference_latency_ms_single_row_cpu: Record<string, { p50: number; p95: number }>;
  notes: string[];
}

const DETECTORS = ["v1_model", "rules", "v2_model", "v2_hybrid"] as const;
const LABEL: Record<string, string> = {
  v1_model: "v1 model",
  rules: "Rules",
  v2_model: "v2 model",
  v2_hybrid: "v2 hybrid (deployed)",
};
const KINDS = ["large", "burst", "new_account"] as const;
const f3 = (v: number | undefined) => (v === undefined ? "—" : v.toFixed(3));

/** Highlight the best value in a row (higher is better unless `lowerIsBetter`). */
function best(values: number[], lowerIsBetter = false): number {
  return lowerIsBetter ? Math.min(...values) : Math.max(...values);
}

function MetricTable({ dataset }: { dataset: Dataset }) {
  const rows: [string, (m: Metrics) => number, boolean][] = [
    ["Precision", (m) => m.precision, false],
    ["Recall", (m) => m.recall, false],
    ["F1", (m) => m.f1, false],
    ["False-positive rate", (m) => m.false_positive_rate, true],
    ...KINDS.map((k): [string, (m: Metrics) => number, boolean] => [`Recall: ${k.replace("_", " ")}`, (m) => m.recall_by_kind[k] ?? 0, false]),
  ];
  return (
    <div className="overflow-x-auto rounded-lg border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Metric</TableHead>
            {DETECTORS.map((d) => (
              <TableHead key={d} className="text-right">
                {LABEL[d]}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map(([label, get, lower]) => {
            const values = DETECTORS.map((d) => get(dataset.detectors[d]!));
            const top = best(values, lower);
            return (
              <TableRow key={label}>
                <TableCell>{label}</TableCell>
                {values.map((v, i) => (
                  <TableCell key={DETECTORS[i]} className={cn("text-right tabular-nums", v === top && "font-semibold")}>
                    {label === "False-positive rate" ? `${(v * 100).toFixed(2)}%` : f3(v)}
                  </TableCell>
                ))}
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </div>
  );
}

function ShiftTable({ report }: { report: Report }) {
  const shifts = Object.entries(report.datasets).filter(([name]) => name.startsWith("shift_"));
  return (
    <div className="overflow-x-auto rounded-lg border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Shifted world</TableHead>
            {DETECTORS.map((d) => (
              <TableHead key={d} className="text-right">
                {LABEL[d]} F1 · burst recall
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {shifts.map(([name, ds]) => (
            <TableRow key={name}>
              <TableCell>
                <span className="font-medium">{name.replace("shift_", "").replace("_", " ")}</span>
                <span className="block text-xs text-muted-foreground">{ds.scenario}</span>
              </TableCell>
              {DETECTORS.map((d) => (
                <TableCell key={d} className="text-right tabular-nums">
                  {f3(ds.detectors[d]!.f1)} · {f3(ds.detectors[d]!.recall_by_kind.burst)}
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function Drift() {
  const drift = useQuery({ queryKey: ["ai", "drift"], queryFn: ({ signal }) => aiApi.drift(signal), staleTime: 30_000 });
  if (!drift.data) return <p className="text-sm text-muted-foreground">{drift.isLoading ? "Loading…" : "Drift report unavailable."}</p>;
  const d = drift.data;
  const tone = d.overall === "stable" ? "success" : d.overall === "moderate" ? "warning" : d.overall === "significant" ? "destructive" : "muted";
  return (
    <div className="space-y-3 text-sm" data-testid="ai-drift">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={tone}>{d.overall.replace("_", " ")}</Badge>
        <span className="text-muted-foreground">
          {d.sample_size} recent transfers scored by {d.model_version}
          {d.live_anomaly_rate !== null && d.live_anomaly_rate !== undefined && ` · flagged ${(d.live_anomaly_rate * 100).toFixed(1)}%`}
          {d.expected_anomaly_rate !== null && d.expected_anomaly_rate !== undefined && ` (model alone on validation: ${(d.expected_anomaly_rate * 100).toFixed(1)}%)`}
        </span>
      </div>
      {d.features.length > 0 && (
        <ul className="grid gap-1 sm:grid-cols-2">
          {d.features.slice(0, 6).map((f) => (
            <li key={f.feature} className="flex justify-between gap-3 rounded border px-2 py-1">
              <span className="truncate">{f.feature}</span>
              <span className={cn("tabular-nums", f.status === "significant" && "text-destructive", f.status === "moderate" && "text-warning")}>
                PSI {f.psi.toFixed(2)}
              </span>
            </li>
          ))}
        </ul>
      )}
      <p className="text-xs text-muted-foreground">{d.note}</p>
    </div>
  );
}

/** Model card: what is deployed and why, measured against v1 and the rule baseline. */
export function ModelEvaluation({ model }: { model: ModelInfo }) {
  const report = model.report as Report | null | undefined;
  const held = report?.datasets.held_out_accounts;
  const latency = report?.inference_latency_ms_single_row_cpu ?? {};
  return (
    <div className="space-y-4 text-sm" data-testid="model-card">
      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <KeyValue label="Deployed">{model.version}</KeyValue>
        <KeyValue label="Decision">{model.decision === "hybrid" ? "model OR rules" : "model only"}</KeyValue>
        <KeyValue label="Threshold">{model.threshold?.toFixed(4)}</KeyValue>
        <KeyValue label="Inference p50 (CPU)">
          {Object.entries(latency)
            .map(([v, l]) => `${v.replace("iforest-", "")} ${l.p50} ms`)
            .join(" · ") || "—"}
        </KeyValue>
      </dl>
      <p className="text-muted-foreground">{model.decision_rule}</p>

      {held ? (
        <>
          <div>
            <h3 className="mb-2 font-medium">Held-out accounts ({held.rows.toLocaleString()} transfers)</h3>
            <MetricTable dataset={held} />
          </div>
          <Disclosure title="Three unseen worlds (fresh seeds): mean ± std" meta="F1">
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              {DETECTORS.map((d) => {
                const s = report.fresh_seeds_summary[d]!;
                return (
                  <KeyValue key={d} label={LABEL[d] ?? d}>
                    {f3(s.f1!.mean)} ± {f3(s.f1!.std)}
                    <span className="block text-xs text-muted-foreground">burst recall {f3(s.recall_by_kind.burst!.mean)}</span>
                  </KeyValue>
                );
              })}
            </div>
          </Disclosure>
          <Disclosure title="Distribution shifts the models never trained on" meta={String(Object.keys(report.datasets).filter((k) => k.startsWith("shift_")).length)}>
            <ShiftTable report={report} />
          </Disclosure>
          <Disclosure title="Where the model and the rules disagree (held-out)">
            <dl className="grid grid-cols-3 gap-3">
              {(["model_only", "rules_only", "both"] as const).map((bucket) => (
                <KeyValue key={bucket} label={bucket.replace("_", " ")}>
                  {Object.entries(report.disagreements_held_out[bucket])
                    .map(([label, n]) => `${n} ${label}`)
                    .join(", ") || "none"}
                </KeyValue>
              ))}
            </dl>
          </Disclosure>
        </>
      ) : (
        <p className="text-muted-foreground">No evaluation report: run python -m app.ai.evaluate.</p>
      )}
      <Disclosure title="Live input drift (PSI vs training data)">
        <Drift />
      </Disclosure>
      <Disclosure title="Findings and limitations">
        <ul className="list-disc space-y-1 pl-5 text-xs text-muted-foreground">
          <li>v1 never flagged a burst (recall 0): its velocity features were absolute, and one extreme feature out of six barely moves an Isolation Forest.</li>
          <li>v2 adds per-account cadence features and gives each feature family its own forest: burst recall ≈ 1 on unseen accounts, 0.88 when bursts slow down.</li>
          <li>The rules restate how the synthetic generator creates anomalies, so they are near an upper bound in-distribution, but a fixed &quot;&lt; 1 s&quot; rule catches only ~30% of slowed bursts.</li>
          <li>The v2 model alone pays ~1% false positives; the deployed hybrid&apos;s threshold was chosen on validation data, where the rules already catch almost everything.</li>
          {model.limitations.map((l) => (
            <li key={l}>{l}</li>
          ))}
          {report?.notes.map((n) => <li key={n}>{n}</li>)}
        </ul>
      </Disclosure>
    </div>
  );
}
