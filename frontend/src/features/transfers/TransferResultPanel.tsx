import { AlertTriangle, CheckCircle2, CircleHelp, Clock, ExternalLink, Loader2, Plus, Repeat, RotateCcw, XCircle } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router";
import { CopyButton, IdText, KeyValue, Money } from "@/components/common/display";
import { EmptyState } from "@/components/common/states";
import { Timeline, type TimelineEvent, type TimelineTone } from "@/components/lab/Timeline";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/misc";
import { useCountdown } from "@/hooks/useCountdown";
import { formatTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { AttemptRecord, TransferAttemptState } from "./transferAttempt";
import type { OutcomeCategory, TransferOutcome } from "./transferOutcome";

const CATEGORY: Record<OutcomeCategory, { label: string; tone: TimelineTone; icon: ReactNode; badge: "success" | "destructive" | "warning" | "info" }> = {
  succeeded: { label: "Succeeded", tone: "success", icon: <CheckCircle2 className="size-5 text-success" />, badge: "success" },
  rejected: { label: "Rejected (final)", tone: "destructive", icon: <XCircle className="size-5 text-destructive" />, badge: "destructive" },
  invalid: { label: "Invalid request", tone: "destructive", icon: <XCircle className="size-5 text-destructive" />, badge: "destructive" },
  retryable: { label: "Not executed · retryable", tone: "warning", icon: <Clock className="size-5 text-warning" />, badge: "warning" },
  ambiguous: { label: "Outcome uncertain", tone: "warning", icon: <CircleHelp className="size-5 text-warning" />, badge: "warning" },
};

export interface TransferResultPanelProps {
  state: TransferAttemptState;
  nameOf: (id: string) => string;
  onRetry: () => void;
  onReplay: () => void;
  onStartNew: () => void;
}

export function TransferResultPanel({ state, nameOf, onRetry, onReplay, onStartNew }: TransferResultPanelProps) {
  const outcome = state.lastOutcome;
  const lastAttempt = state.attempts.at(-1);
  const retryDeadline =
    outcome?.retryAfterSeconds && lastAttempt?.finishedAt ? lastAttempt.finishedAt + outcome.retryAfterSeconds * 1000 : null;
  const waitSeconds = useCountdown(state.keyStatus === "pinned" ? retryDeadline : null);

  return (
    <Card aria-live="polite">
      <CardHeader>
        <CardTitle>Transfer result</CardTitle>
        <CardDescription>What the API answered, and what it means for the idempotency key.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">
        {state.inFlight && (
          <div role="status" className="flex items-center gap-2 rounded-lg border bg-muted/30 px-4 py-3 text-sm">
            <Loader2 className="size-4 animate-spin" /> Waiting for the API… (POST /api/v1/transfers)
          </div>
        )}

        {!outcome && !state.inFlight && (
          <EmptyState
            title="No transfer sent yet"
            description="Results appear here: HTTP status, transaction id, serving replica and what to do next."
            className="py-6"
          />
        )}

        {outcome && lastAttempt && (
          <OutcomeSummary outcome={outcome} attempt={lastAttempt} nameOf={nameOf} waitSeconds={waitSeconds} />
        )}

        {outcome && !state.inFlight && (
          <div className="flex flex-wrap gap-2">
            {state.keyStatus === "pinned" && (
              <Button onClick={onRetry} disabled={waitSeconds > 0}>
                <RotateCcw />
                {waitSeconds > 0 ? `Retry with same key in ${waitSeconds} s` : "Retry with same key"}
              </Button>
            )}
            {state.keyStatus === "consumed" && state.boundRequest && (
              <Button variant="outline" onClick={onReplay} title="Send the identical request with the identical key">
                <Repeat /> Send again with same key
              </Button>
            )}
            {outcome.category === "succeeded" && outcome.transfer && (
              <Button variant="outline" asChild>
                <Link to={`/history?transfer=${outcome.transfer.id}`}>
                  <ExternalLink /> View ledger entries
                </Link>
              </Button>
            )}
            <Button variant={state.keyStatus === "pinned" ? "ghost" : "secondary"} onClick={onStartNew}>
              <Plus /> New transfer (new key)
            </Button>
          </div>
        )}
        {state.keyStatus === "pinned" && outcome?.category === "ambiguous" && !state.inFlight && (
          <p className="flex items-start gap-2 text-xs text-warning">
            <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
            Starting a new transfer instead of retrying could move the money twice if the first attempt did commit.
          </p>
        )}

        {state.attempts.length > 0 && (
          <>
            <Separator />
            <div className="space-y-3">
              <div className="text-xs font-medium text-muted-foreground">Attempts for this logical transfer</div>
              <Timeline events={state.attempts.map(toTimelineEvent)} />
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}

function OutcomeSummary({
  outcome,
  attempt,
  nameOf,
  waitSeconds,
}: {
  outcome: TransferOutcome;
  attempt: AttemptRecord;
  nameOf: (id: string) => string;
  waitSeconds: number;
}) {
  const style = CATEGORY[outcome.category];
  const request = attempt.request;
  const transfer = outcome.transfer;
  return (
    <div className="space-y-4" data-testid="transfer-outcome" data-category={outcome.category}>
      <div className="flex items-start gap-3">
        <span className="mt-0.5">{style.icon}</span>
        <div className="min-w-0 flex-1 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-base font-semibold">{outcome.title}</h3>
            <Badge variant={style.badge}>{style.label}</Badge>
            {outcome.replayed && <Badge variant="info">Idempotent-Replayed</Badge>}
          </div>
          <p className="text-sm text-muted-foreground">{outcome.message}</p>
        </div>
      </div>

      <div
        className={cn(
          "rounded-lg border px-4 py-3 text-sm",
          outcome.category === "succeeded" && "border-success/30 bg-success/5",
          (outcome.category === "rejected" || outcome.category === "invalid") && "border-destructive/30 bg-destructive/5",
          (outcome.category === "retryable" || outcome.category === "ambiguous") && "border-warning/30 bg-warning/5",
        )}
      >
        <span className="font-medium">What next: </span>
        {outcome.guidance}
        {outcome.status === 429 && (
          <div className="mt-1 font-medium text-warning">
            {outcome.retryAfterSeconds !== null
              ? waitSeconds > 0
                ? `Retry-After: ${outcome.retryAfterSeconds} s — you can retry in ${waitSeconds} s.`
                : `Retry-After (${outcome.retryAfterSeconds} s) has elapsed — you can retry now.`
              : "No Retry-After header was returned; wait a moment before retrying."}
          </div>
        )}
      </div>

      <dl className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3">
        <KeyValue label="HTTP status">
          <code className="font-mono">{outcome.status ?? "no response"}</code>
        </KeyValue>
        <KeyValue label="Error / status code">
          <code className="font-mono text-xs">{outcome.code}</code>
        </KeyValue>
        <KeyValue label="Transfer status">{transfer ? <Badge variant="success">{transfer.status}</Badge> : <span className="text-muted-foreground">—</span>}</KeyValue>
        <KeyValue label="Transaction id">{transfer ? <IdText id={transfer.id} /> : <span className="text-muted-foreground">none</span>}</KeyValue>
        <KeyValue label="Amount">
          <Money minor={transfer?.amount_minor ?? request.amount_minor} />
        </KeyValue>
        <KeyValue label="Route" className="col-span-2 sm:col-span-1">
          <span className="block truncate" title={`${nameOf(request.source_account_id)} → ${nameOf(request.destination_account_id)}`}>
            {nameOf(request.source_account_id)} → {nameOf(request.destination_account_id)}
          </span>
        </KeyValue>
        <KeyValue label="Idempotency key" className="col-span-2">
          <span className="flex items-center gap-1">
            <code className="truncate font-mono text-xs">{attempt.key}</code>
            <CopyButton value={attempt.key} label="Copy idempotency key" />
          </span>
        </KeyValue>
        <KeyValue label="Served by">
          <code className="font-mono text-xs">{outcome.meta?.servedBy ?? "—"}</code>
        </KeyValue>
        <KeyValue label="Request id" className="col-span-2">
          <code className="truncate font-mono text-xs">{outcome.meta?.requestId ?? "—"}</code>
        </KeyValue>
        <KeyValue label="Latency">
          <code className="font-mono text-xs">{outcome.meta ? `${outcome.meta.durationMs} ms` : "—"}</code>
        </KeyValue>
      </dl>

      {outcome.meta?.rateLimit.policy && (
        <p className="text-xs text-muted-foreground">
          Rate limit policy <code className="font-mono">{outcome.meta.rateLimit.policy}</code>: {outcome.meta.rateLimit.remaining ?? "?"} of{" "}
          {outcome.meta.rateLimit.limit ?? "?"} tokens left (shared by every replica via Redis).
        </p>
      )}

      {outcome.details !== null && outcome.details !== undefined && (
        <details className="rounded-lg border bg-muted/30">
          <summary className="cursor-pointer px-3 py-2 text-xs font-medium">Error details</summary>
          <pre className="overflow-x-auto px-3 pb-3 font-mono text-xs text-muted-foreground">{JSON.stringify(outcome.details, null, 2)}</pre>
        </details>
      )}
    </div>
  );
}

function toTimelineEvent(record: AttemptRecord): TimelineEvent {
  const o = record.outcome;
  const modeLabel = record.mode === "retry" ? "Retry" : record.mode === "replay" ? "Replay" : "Submit";
  return {
    id: `${record.attempt}`,
    tone: o ? CATEGORY[o.category].tone : "info",
    title: (
      <span>
        #{record.attempt} {modeLabel} · {o ? `${o.status ?? "no response"} ${o.code}` : "in flight…"}
        {o?.replayed && <span className="text-info"> · replayed</span>}
      </span>
    ),
    time: formatTime(record.startedAt),
    body: (
      <span>
        key <code className="font-mono">{record.key.slice(0, 8)}…</code>
        {o?.meta?.servedBy && <> · served by {o.meta.servedBy}</>}
        {o?.meta && <> · {o.meta.durationMs} ms</>}
      </span>
    ),
  };
}
