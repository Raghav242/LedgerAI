import { Route } from "lucide-react";
import { Disclosure } from "@/components/common/Disclosure";
import { flowForOutcome, idleFlow, OUTCOME_LABEL, outcomeForCode } from "@/components/lab/flow/flowModel";
import { RequestFlowDiagram } from "@/components/lab/flow/RequestFlowDiagram";
import type { TransferAttemptState } from "./transferAttempt";

const KNOWN_REPLICAS = ["api-1", "api-2"];

/**
 * The path of the latest attempt, collapsed by default: derived from its response (status,
 * error code, X-Served-By, Idempotent-Replayed, X-RateLimit-*), not traced.
 */
export function TransferFlowCard({
  state,
  open,
  onOpenChange,
}: {
  state: TransferAttemptState;
  /** Owned by the page so the user's choice survives starting a new transfer. */
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const outcome = state.lastOutcome;
  const last = state.attempts.at(-1);
  const replica = outcome?.meta?.servedBy ?? null;
  const replicas = replica && !KNOWN_REPLICAS.includes(replica) ? [...KNOWN_REPLICAS, replica].sort() : KNOWN_REPLICAS;
  const flowOutcome = outcome ? outcomeForCode(outcome.status, outcome.code, outcome.replayed) : null;

  const model = state.inFlight
    ? { ...idleFlow(replicas), caption: "Waiting for the response…" }
    : outcome && flowOutcome
      ? flowForOutcome(flowOutcome, {
          replica,
          replicas,
          status: outcome.status,
          requestId: !!outcome.meta?.requestId,
          rateHeaders: !!outcome.meta?.rateLimit.policy,
          details:
            outcome.meta && outcome.meta.rateLimit.remaining !== null && outcome.status !== 429
              ? { redis: `${outcome.meta.rateLimit.remaining}/${outcome.meta.rateLimit.limit ?? "?"} tokens left` }
              : undefined,
        })
      : idleFlow(replicas);

  const meta = state.inFlight ? "in flight…" : flowOutcome ? `${OUTCOME_LABEL[flowOutcome]}${replica ? ` · ${replica}` : ""}` : undefined;

  return (
    <Disclosure variant="boxed" className="bg-card" icon={<Route />} title="Request path" meta={meta} open={open} onOpenChange={onOpenChange}>
      <RequestFlowDiagram
        model={model}
        playKey={last ? `${last.attempt}-${last.finishedAt ?? "pending"}` : "idle"}
        animate={!!outcome && !state.inFlight}
        showAsync={flowOutcome === "committed"}
      />
    </Disclosure>
  );
}
