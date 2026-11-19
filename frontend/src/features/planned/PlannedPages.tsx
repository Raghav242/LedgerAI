import { ArrowDown, ArrowRight, CircleDashed, Construction, Terminal } from "lucide-react";
import type { ReactNode } from "react";
import { PageHeader } from "@/components/common/display";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/misc";
import { cn } from "@/lib/utils";

function PlannedNotice({ phase, children }: { phase: string; children: ReactNode }) {
  return (
    <Alert variant="info" role="note">
      <Construction />
      <AlertTitle>Planned for Phase {phase}: not functional yet</AlertTitle>
      <AlertDescription>{children}</AlertDescription>
    </Alert>
  );
}

// --- Experiment Lab -----------------------------------------------------------------------

interface Experiment {
  name: string;
  question: string;
  invariant: string;
  /** An existing CLI demo in scripts/ that already runs this experiment today, if any. */
  script?: string;
}

const EXPERIMENTS: Experiment[] = [
  {
    name: "Concurrent withdrawals",
    question: "100 concurrent $10 transfers from a $100 account: how many succeed?",
    invariant: "Exactly 10 succeed; balance never negative; reconciliation passes.",
    script: "scripts/demo_concurrency.py",
  },
  {
    name: "Idempotent retries",
    question: "20 concurrent copies of one request with the same key.",
    invariant: "Exactly one transfer; every other response is a replay.",
    script: "scripts/demo_idempotency.py",
  },
  {
    name: "Distributed rate limiting",
    question: "Burst writes against a global token bucket shared by all replicas.",
    invariant: "Admitted requests never exceed the global quota; rejected ones carry Retry-After.",
    script: "scripts/demo_rate_limiting.py",
  },
  {
    name: "Load balancing",
    question: "How NGINX spreads requests over replicas, and what happens when one stops.",
    invariant: "No failed requests beyond in-flight ones; state survives the replica.",
    script: "scripts/demo_load_balancing.py",
  },
  {
    name: "Redis outage",
    question: "Stop Redis mid-traffic.",
    invariant: "Writes fail closed fast (503), reads stay up, clean recovery.",
    script: "scripts/demo_redis_failure.py",
  },
  {
    name: "RabbitMQ outage",
    question: "Stop the broker while transfers continue.",
    invariant: "Transfers keep succeeding; events wait in the outbox and are delivered once later.",
    script: "scripts/demo_outbox_recovery.py",
  },
  {
    name: "Worker crash and redelivery",
    question: "Kill the consumer before and after its commit.",
    invariant: "Each event's effect is applied exactly once despite redelivery.",
    script: "scripts/demo_worker_crash.py",
  },
  {
    name: "AI anomaly detection",
    question: "Inject unusual transfer patterns and inspect anomaly scores.",
    invariant: "Requires the Phase 4B anomaly service (not implemented).",
  },
  {
    name: "Performance benchmarks",
    question: "Throughput and latency with 1, 2 and 4 API replicas.",
    invariant: "Locust workloads with measured, reproducible results.",
    script: "benchmarks/run_benchmarks.py",
  },
];

export function ExperimentLabPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="Experiment Lab"
        description="Controlled experiments that make concurrency, idempotency and failure handling observable, each ending in explicit invariant checks."
      />
      <PlannedNotice phase="5B">
        Experiments cannot be run from the browser yet. Several already exist as command-line demos that run against the local stack and verify
        their invariants; Phase 5B will add a safe, local-only way to run them from here and stream results.
      </PlannedNotice>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
        {EXPERIMENTS.map((e) => (
          <Card key={e.name} className="gap-3">
            <CardHeader>
              <CardTitle>{e.name}</CardTitle>
              <CardDescription>{e.question}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3 text-sm">
              <div>
                <div className="text-xs font-medium text-muted-foreground">Invariant checked</div>
                <div>{e.invariant}</div>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant="muted">
                  <CircleDashed /> Planned
                </Badge>
                {e.script ? (
                  <Badge variant="outline" className="font-mono font-normal" title="Exists today as a CLI demo">
                    <Terminal /> {e.script}
                  </Badge>
                ) : (
                  <Badge variant="outline" className="border-dashed font-normal text-muted-foreground">
                    Backend not built
                  </Badge>
                )}
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}

// --- System Topology ----------------------------------------------------------------------

type NodeStatus = "implemented" | "optional" | "planned";

function TopoNode({ name, detail, status }: { name: string; detail: string; status: NodeStatus }) {
  return (
    <div
      className={cn(
        "min-w-0 rounded-lg border bg-card px-3 py-2.5",
        status === "planned" && "border-dashed bg-transparent text-muted-foreground",
        status === "optional" && "border-dashed",
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-sm font-medium">{name}</span>
        <Badge variant={status === "implemented" ? "success" : status === "optional" ? "outline" : "muted"} className="text-[10px]">
          {status}
        </Badge>
      </div>
      <div className="mt-0.5 text-xs text-muted-foreground">{detail}</div>
    </div>
  );
}

function Down() {
  return (
    <div className="flex justify-center py-1 text-muted-foreground" aria-hidden>
      <ArrowDown className="size-4" />
    </div>
  );
}

export function TopologyPage() {
  return (
    <div className="space-y-6">
      <PageHeader title="System Topology" description="The components in this repository and how a request and its event flow through them." />
      <PlannedNotice phase="5C">
        This is a static architecture diagram derived from docker-compose.yml. It shows no live health data. Live status polling and per-replica
        traffic will arrive in Phase 5C.
      </PlannedNotice>
      <Card>
        <CardHeader>
          <CardTitle>Request and event path</CardTitle>
          <CardDescription>Solid: implemented and running in the default stack. Dashed: optional profile or planned.</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="mx-auto grid max-w-3xl gap-0">
            <TopoNode name="React frontend" detail="This app (Vite, port 5173), proxies /api to the gateway" status="implemented" />
            <Down />
            <TopoNode name="NGINX gateway" detail="Round-robin load balancer, request ids, port 8080" status="implemented" />
            <Down />
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <TopoNode name="api-1" detail="FastAPI replica" status="implemented" />
              <TopoNode name="api-2" detail="FastAPI replica" status="implemented" />
              <TopoNode name="api-3" detail="--profile scale4" status="optional" />
              <TopoNode name="api-4" detail="--profile scale4" status="optional" />
            </div>
            <Down />
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              <TopoNode name="Redis" detail="Global token-bucket rate limits only (no financial data)" status="implemented" />
              <TopoNode name="PostgreSQL" detail="Balances, ledger, idempotency records, transactional outbox" status="implemented" />
            </div>
            <Down />
            <TopoNode name="outbox-publisher" detail="Polls committed outbox rows (SKIP LOCKED, lease), publishes with confirms" status="implemented" />
            <Down />
            <TopoNode name="RabbitMQ" detail="Durable queues, retry tiers and a dead-letter queue (single node)" status="implemented" />
            <Down />
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              <TopoNode name="event-consumer" detail="Idempotent audit consumer: exactly-once effect via receipts" status="implemented" />
              <TopoNode name="AI anomaly worker" detail="Isolation Forest scoring of transfer events (Phase 4B)" status="planned" />
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

// --- AI Anomaly Lab -----------------------------------------------------------------------

export function AnomalyLabPage() {
  return (
    <div className="space-y-6">
      <PageHeader title="AI Anomaly Lab" description="Machine-learning anomaly detection on the stream of transfer events." />
      <PlannedNotice phase="4B / 5">
        The backend does not implement anomaly detection yet, so there is nothing to show here. The repository has the event pipeline it will
        build on (transactional outbox, RabbitMQ, an idempotent consumer), but no Isolation Forest model, anomaly storage or anomaly API.
      </PlannedNotice>
      <Card>
        <CardHeader>
          <CardTitle>Planned design</CardTitle>
          <CardDescription>What this page will show once the backend exists.</CardDescription>
        </CardHeader>
        <CardContent>
          <ul className="space-y-2 text-sm text-muted-foreground">
            {[
              "An AI worker consumes transfer.completed events from RabbitMQ (deduplicated by event id, like the audit consumer).",
              "Features per transfer: amount, account velocity, time of day, counterparty novelty.",
              "An Isolation Forest scores each transfer; scores and flags are stored with the model version.",
              "Read-only anomaly APIs list flagged transfers with their feature values and score.",
              "This page: score distribution, flagged transfers linked to their ledger entries, and labelled experiment runs.",
            ].map((line) => (
              <li key={line} className="flex gap-2">
                <ArrowRight className="mt-0.5 size-4 shrink-0" /> {line}
              </li>
            ))}
          </ul>
        </CardContent>
      </Card>
    </div>
  );
}

// --- Performance Lab ----------------------------------------------------------------------

export function PerformanceLabPage() {
  return (
    <div className="space-y-6">
      <PageHeader title="Performance Lab" description="Compare throughput and latency across system configurations." />
      <PlannedNotice phase="5D">
        No benchmark data is displayed here yet. Measured Locust results for 1, 2 and 4 replicas already exist in the repository
        (docs/phase-3-design.md, section 13, and benchmarks/results/); Phase 5D will chart them and compare new runs.
      </PlannedNotice>
      <Card>
        <CardHeader>
          <CardTitle>Run the existing benchmarks locally</CardTitle>
          <CardDescription>Redeploys the stack against a separate database, then restores it.</CardDescription>
        </CardHeader>
        <CardContent>
          <pre className="overflow-x-auto rounded-lg border bg-muted/30 p-3 font-mono text-xs">
            python benchmarks/run_benchmarks.py --duration 30 --users 64 --replicas 1 2 4
          </pre>
        </CardContent>
      </Card>
    </div>
  );
}
