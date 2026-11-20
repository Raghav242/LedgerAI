import { ArrowRight, Construction } from "lucide-react";
import type { ReactNode } from "react";
import { PageHeader } from "@/components/common/display";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/misc";

function PlannedNotice({ phase, children }: { phase: string; children: ReactNode }) {
  return (
    <Alert variant="info" role="note">
      <Construction />
      <AlertTitle>Planned for Phase {phase}: not functional yet</AlertTitle>
      <AlertDescription>{children}</AlertDescription>
    </Alert>
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
