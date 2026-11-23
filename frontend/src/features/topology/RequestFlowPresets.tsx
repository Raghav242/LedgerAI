import { FlaskConical } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";
import { Disclosure } from "@/components/common/Disclosure";
import { Tabs } from "@/components/common/Tabs";
import { aggregateFlow, asIllustration, flowForOutcome, type FlowModel } from "@/components/lab/flow/flowModel";
import { ReplicaDistribution } from "@/components/lab/flow/ReplicaDistribution";
import { RequestFlowDiagram, type RequestFlowDiagramProps } from "@/components/lab/flow/RequestFlowDiagram";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { tabPanelProps } from "@/lib/a11y";

const REPLICAS = ["api-1", "api-2"];
const LB_SEQUENCE = ["api-1", "api-2", "api-1", "api-2", "api-1", "api-2", "api-1", "api-2"];

type PresetId = "transfer" | "replay" | "rate-limit" | "insufficient" | "load-balancing";

interface Preset {
  id: PresetId;
  label: string;
  model: FlowModel;
  steps: string[];
  experiment?: string;
  showAsync: boolean;
  mode: "single" | "aggregate";
}

const PRESETS: Preset[] = [
  {
    id: "transfer",
    label: "Transfer",
    model: asIllustration(flowForOutcome("committed", { replica: "api-1", replicas: REPLICAS, status: 201 })),
    showAsync: true,
    mode: "single",
    experiment: "concurrency",
    steps: [
      "NGINX picks the next replica (round robin) and adds X-Request-ID.",
      "The replica takes one token from the shared write bucket in Redis.",
      "One PostgreSQL transaction: claim the Idempotency-Key, lock both account rows in id order, check funds, update balances.",
      "Insert the transfer, one debit and one credit ledger entry, and a transfer.completed outbox row.",
      "COMMIT once: money, ledger, event and the key's stored response become durable together. 201 Created.",
      "Later, the outbox publisher sends the event to RabbitMQ and the consumer applies it exactly once.",
    ],
  },
  {
    id: "replay",
    label: "Idempotent replay",
    model: asIllustration(flowForOutcome("replayed", { replica: "api-2", replicas: REPLICAS, status: 201 })),
    showAsync: false,
    mode: "single",
    experiment: "idempotency",
    steps: [
      "A retry arrives with the same Idempotency-Key and the same body, possibly at the other replica.",
      "The claim INSERT finds the key already completed (it waits if the first request is still running).",
      "The stored response is returned byte for byte with Idempotent-Replayed: true.",
      "No row lock, no balance change, no new ledger entry, no new event.",
    ],
  },
  {
    id: "rate-limit",
    label: "Rate-limit rejection",
    model: asIllustration(flowForOutcome("rate_limited", { replica: "api-1", replicas: REPLICAS, status: 429 })),
    showAsync: false,
    mode: "single",
    experiment: "rate_limiting",
    steps: [
      "The replica asks Redis for a token: one atomic Lua script on the bucket shared by every replica.",
      "The bucket is empty, so the request is refused before a database session is even opened.",
      "429 RATE_LIMITED with Retry-After and X-RateLimit-* headers; retrying later with the same key is safe.",
    ],
  },
  {
    id: "insufficient",
    label: "Insufficient funds",
    model: asIllustration(flowForOutcome("insufficient_funds", { replica: "api-2", replicas: REPLICAS, status: 409 })),
    showAsync: false,
    mode: "single",
    experiment: "concurrency",
    steps: [
      "Admitted by the rate limiter; the key is claimed.",
      "Both rows are locked; the current balance is too low.",
      "Rolled back to a savepoint, then the rejection is stored as the key's final outcome and committed.",
      "409 INSUFFICIENT_FUNDS. Retrying with the same key replays the same rejection.",
    ],
  },
  {
    id: "load-balancing",
    label: "Load balancing",
    model: asIllustration(
      aggregateFlow(
        LB_SEQUENCE.map((replica) => ({ outcome: "read_ok" as const, replica })),
        "Eight reads: NGINX alternates between the replicas; both answer identically because all state is in PostgreSQL and Redis.",
        REPLICAS,
      ),
    ),
    showAsync: false,
    mode: "aggregate",
    experiment: "load_balancing",
    steps: [
      "NGINX is the only published entry point; replicas are reachable only on the internal network.",
      "Each new request goes to the next replica in turn. A replica that refuses connections is skipped for GETs.",
      "Replicas are stateless: any replica can serve any request, so adding replicas adds capacity.",
    ],
  },
];

/** Deployment facts per drawn component, shown in the node details panel. From docker-compose.yml. */
const COMPONENT_INFO: RequestFlowDiagramProps["componentInfo"] = {
  client: "This React app (Vite on :5173) proxies /api and /health to NGINX, so the browser never needs CORS.",
  nginx: "nginx:1.28 on :8080, the only published API port. Retries connect failures for safe requests only.",
  replica: "Compose services api-1 and api-2 (default). api-3 and api-4 are optional (--profile scale4, NGINX_UPSTREAMS=4).",
  redis: "redis:7.4, no persistence: rate-limit state is ephemeral and holds no financial data.",
  database: "postgres:16. Accounts, transfers, the double-entry ledger, idempotency records, the transactional outbox and lab runs.",
  outbox: "The outbox-publisher service claims committed rows with SKIP LOCKED under a lease and publishes them with confirms.",
  rabbitmq: "rabbitmq:4.1. Durable quorum queues, retry tiers and a dead-letter queue. Single node, not highly available.",
  worker: "The event-consumer service (and the AI anomaly worker), each deduplicating by event id.",
};

/** Documented request paths (illustrations), one tab per common flow. Secondary to the live view. */
export function RequestFlowPresets() {
  const [presetId, setPresetId] = useState<PresetId>("transfer");
  const preset = PRESETS.find((p) => p.id === presetId) ?? PRESETS[0]!;
  return (
    <Card className="gap-0 py-0">
      <Tabs<PresetId>
        idPrefix="topology"
        label="Flows"
        value={preset.id}
        onChange={setPresetId}
        className="px-2 pt-1"
        items={PRESETS.map((p) => ({ id: p.id, label: p.label }))}
      />
      <CardContent {...tabPanelProps("topology", preset.id)} className="space-y-4 p-4 outline-none">
        <RequestFlowDiagram
          key={preset.id}
          model={preset.model}
          mode={preset.mode}
          playKey={preset.id}
          showAsync={preset.showAsync}
          componentInfo={COMPONENT_INFO}
          label={`${preset.label} flow`}
        />
        {preset.id === "load-balancing" && <ReplicaDistribution sequence={LB_SEQUENCE} counts={{ "api-1": 4, "api-2": 4 }} />}
        <div className="flex flex-wrap items-start justify-between gap-3">
          <Disclosure title="Step by step" meta={`${preset.steps.length} steps`} className="min-w-0 flex-1">
            <ol className="list-decimal space-y-1.5 pl-5 text-sm">
              {preset.steps.map((step) => (
                <li key={step}>{step}</li>
              ))}
            </ol>
          </Disclosure>
          {preset.experiment && (
            <Button variant="outline" size="sm" asChild>
              <Link to={`/experiments?experiment=${preset.experiment}`}>
                <FlaskConical /> Try it in the Experiment Lab
              </Link>
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
