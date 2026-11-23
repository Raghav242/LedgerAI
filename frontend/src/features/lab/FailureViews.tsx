/**
 * Result panels for the Phase 5D failure experiments (labctl/experiments.py defines the
 * `result.data` shapes). The default visual is the disturbed component's status before,
 * during and after; the details tab adds the evidence specific to each experiment.
 */
import { ArrowRight } from "lucide-react";
import { KeyValue } from "@/components/common/display";
import { ReplicaDistribution } from "@/components/lab/flow/ReplicaDistribution";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { StatusLabel } from "@/features/observability/health";
import type { ComponentStatus, LabRun } from "@/types/api";

type Data = Record<string, unknown>;

const obj = (v: unknown): Data => (typeof v === "object" && v !== null && !Array.isArray(v) ? (v as Data) : {});
const arr = (v: unknown): Data[] => (Array.isArray(v) ? (v as Data[]) : []);
const KNOWN: ComponentStatus[] = ["healthy", "degraded", "unhealthy", "stale", "unknown", "not_configured"];

/** Before → during → after, for the one component the experiment disturbed. */
export function PhaseStatusStrip({ data }: { data: Data }) {
  const phases = arr(data.component_status);
  if (data.aborted) return <p className="text-sm text-muted-foreground">Aborted before disturbing anything: the stack was not healthy.</p>;
  if (!phases.length) return null;
  return (
    <div className="space-y-1.5" data-testid="phase-status">
      <div className="text-xs text-muted-foreground">{String(data.component ?? "component")} status</div>
      <ol className="flex flex-wrap items-center gap-2">
        {phases.map((p, i) => {
          const status = (KNOWN.includes(p.status as ComponentStatus) ? p.status : "unknown") as ComponentStatus;
          return (
            <li key={String(p.phase)} className="flex items-center gap-2">
              {i > 0 && <ArrowRight className="size-3.5 text-muted-foreground" aria-hidden />}
              <span className="rounded-md border px-2.5 py-1.5">
                <span className="block text-[11px] capitalize text-muted-foreground">{String(p.phase)}</span>
                <StatusLabel status={status} />
              </span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function RedisView({ data }: { data: Data }) {
  return (
    <div className="space-y-4" data-testid="result-redis-outage">
      <PhaseStatusStrip data={data} />
      <dl className="grid grid-cols-2 gap-4">
        <KeyValue label="Redis stopped for">{data.outage_seconds !== undefined ? `${String(data.outage_seconds)} s` : "—"}</KeyValue>
        <KeyValue label="Ready again after restart">{data.recovery_seconds !== null && data.recovery_seconds !== undefined ? `${String(data.recovery_seconds)} s` : "did not recover"}</KeyValue>
      </dl>
    </div>
  );
}

function RabbitView({ data }: { data: Data }) {
  const events = arr(data.events);
  return (
    <div className="space-y-4" data-testid="result-rabbitmq-outage">
      <PhaseStatusStrip data={data} />
      {events.length > 0 && (
        <div className="overflow-x-auto rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Event</TableHead>
                <TableHead>During outage</TableHead>
                <TableHead>After</TableHead>
                <TableHead className="text-right">Publish attempts</TableHead>
                <TableHead className="text-right">Receipts / effects</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {events.map((e) => (
                <TableRow key={String(e.event_id)}>
                  <TableCell className="font-mono text-xs">{String(e.event_id).slice(0, 8)}…</TableCell>
                  <TableCell>{String(e.during ?? "—")}</TableCell>
                  <TableCell>{String(e.after ?? "—")}</TableCell>
                  <TableCell className="text-right tabular-nums">{String(e.publish_attempts ?? "—")}</TableCell>
                  <TableCell className="text-right tabular-nums">
                    {String(e.receipts ?? 0)} / {String(e.effects ?? 0)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
      <p className="text-xs text-muted-foreground">
        A 201 means the transfer committed. Delivery is a separate, later fact: {String(data.publisher_during ?? "")}
      </p>
    </div>
  );
}

function WorkerView({ data }: { data: Data }) {
  const scenarios = arr(data.scenarios);
  return (
    <div className="space-y-3" data-testid="result-worker-crash">
      <div className="overflow-x-auto rounded-lg border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Crash point</TableHead>
              <TableHead>Process died</TableHead>
              <TableHead className="text-right">Restarts</TableHead>
              <TableHead className="text-right">Effects</TableHead>
              <TableHead>Duplicate recognised</TableHead>
              <TableHead className="text-right">Settled</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {scenarios.map((s) => (
              <TableRow key={String(s.event_id)}>
                <TableCell>{String(s.crash_point)}</TableCell>
                <TableCell>{s.crashed ? "yes" : "no"}</TableCell>
                <TableCell className="text-right tabular-nums">{String(s.process_restarts)}</TableCell>
                <TableCell className="text-right tabular-nums">{String(s.effects)}</TableCell>
                <TableCell>{s.duplicate_detected ? "yes (receipt found)" : "no (rolled back)"}</TableCell>
                <TableCell className="text-right tabular-nums">{s.settled_seconds !== null ? `${String(s.settled_seconds)} s` : "timed out"}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}

function ReplicaView({ data }: { data: Data }) {
  const sequences = obj(data.sequences);
  return (
    <div className="space-y-4" data-testid="result-replica-failure">
      <PhaseStatusStrip data={data} />
      {(["before", "during", "after"] as const).map((phase) => {
        const seq = Array.isArray(sequences[phase]) ? (sequences[phase] as string[]) : [];
        if (!seq.length) return null;
        const counts: Record<string, number> = {};
        for (const name of seq) counts[name] = (counts[name] ?? 0) + 1;
        return (
          <section key={phase} aria-label={`${phase} the failure`}>
            <h4 className="mb-1 text-xs font-medium capitalize text-muted-foreground">{phase}</h4>
            <ReplicaDistribution sequence={seq} counts={counts} />
          </section>
        );
      })}
    </div>
  );
}

export function FailureResultView({ run }: { run: LabRun }) {
  const data = obj(run.result?.data);
  switch (run.experiment) {
    case "redis_outage":
      return <RedisView data={data} />;
    case "rabbitmq_outage":
      return <RabbitView data={data} />;
    case "worker_crash":
      return <WorkerView data={data} />;
    case "replica_failure":
      return <ReplicaView data={data} />;
    default:
      return null;
  }
}

/** Compact visual for the result summary. */
export function FailureVisual({ run }: { run: LabRun }) {
  const data = obj(run.result?.data);
  if (run.experiment === "worker_crash") {
    const scenarios = arr(data.scenarios);
    return (
      <p className="text-sm" data-testid="phase-status">
        {scenarios.map((s) => `${String(s.crash_point)}: died ${s.crashed ? "✓" : "✗"}, effects ${String(s.effects)}`).join(" · ")}
      </p>
    );
  }
  return <PhaseStatusStrip data={data} />;
}
