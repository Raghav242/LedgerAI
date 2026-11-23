import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Cog, Inbox, ListOrdered, Send, Workflow } from "lucide-react";
import { useState, type ReactNode } from "react";
import { isApiError } from "@/api/client";
import { observabilityApi, type EventStatusFilter } from "@/api/observability";
import { queryKeys } from "@/api/queryKeys";
import { Disclosure } from "@/components/common/Disclosure";
import { IdText, KeyValue, Money, PageHeader } from "@/components/common/display";
import { StatusSummary } from "@/components/common/StatusSummary";
import { EmptyState, ErrorState, LoadingRows } from "@/components/common/states";
import { Tabs } from "@/components/common/Tabs";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/dialog";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ago, secondsSince } from "@/features/observability/healthMeta";
import { LifecycleExplorer } from "@/features/observability/LifecycleExplorer";
import { formatTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { EventSummary, PipelineSummary } from "@/types/api";

const PAGE = 25;

function Stage({ icon, title, value, sub, tone }: { icon: ReactNode; title: string; value: ReactNode; sub?: ReactNode; tone?: "warning" | "destructive" }) {
  return (
    <div
      className={cn(
        "flex min-w-0 flex-1 items-center gap-3 rounded-lg border bg-card px-3 py-2.5",
        tone === "warning" && "border-warning/50 bg-warning/5",
        tone === "destructive" && "border-destructive/50 bg-destructive/5",
      )}
    >
      <span className="text-muted-foreground [&>svg]:size-4" aria-hidden>
        {icon}
      </span>
      <div className="min-w-0">
        <div className="text-xs text-muted-foreground">{title}</div>
        <div className="text-lg font-semibold tabular-nums">{value}</div>
        {sub && <div className="truncate text-[11px] text-muted-foreground">{sub}</div>}
      </div>
    </div>
  );
}

function Arrow() {
  return <ArrowRight className="hidden size-4 shrink-0 text-muted-foreground md:block" aria-hidden />;
}

function verdict(s: PipelineSummary) {
  const waiting = s.outbox_pending + s.outbox_processing;
  const dlq = s.consumers.flatMap((c) => c.queues).filter((q) => q.role === "dead_letter").reduce((n, q) => n + q.ready, 0);
  if (s.awaiting_retry > 0)
    return { tone: "warning" as const, title: `${s.awaiting_retry} event(s) failed to publish and will be retried`, body: s.publisher_last_error ?? "The publisher could not reach RabbitMQ. The events are safe in the outbox." };
  if (waiting > 0 && (s.oldest_unpublished_age_seconds ?? 0) > 30)
    return { tone: "warning" as const, title: `${waiting} event(s) waiting, the oldest for ${ago(s.oldest_unpublished_age_seconds)}`, body: "Committed transfers are not affected; their events wait in the outbox until a publisher delivers them." };
  if (dlq > 0) return { tone: "warning" as const, title: `${dlq} message(s) in a dead-letter queue`, body: "They failed permanently or exhausted their retries." };
  return { tone: "success" as const, title: "Events are flowing", body: waiting ? `${waiting} event(s) being published right now.` : "Every committed event has been published." };
}

function StatusCell({ e }: { e: EventSummary }) {
  if (e.status === "published") return <Badge variant="success">Published</Badge>;
  if (e.last_error) return <Badge variant="destructive">Retrying</Badge>;
  return <Badge variant="outline">{e.status === "processing" ? "Publishing" : "Waiting"}</Badge>;
}

/** Outbox → publisher → RabbitMQ → consumers, from persisted state and worker reports. */
export function EventPipelinePage() {
  const [filter, setFilter] = useState<EventStatusFilter>("all");
  const [cursors, setCursors] = useState<string[]>([]);
  const [selected, setSelected] = useState<EventSummary | null>(null);
  const cursor = cursors.at(-1);

  const summary = useQuery({
    queryKey: queryKeys.observability.pipeline,
    queryFn: ({ signal }) => observabilityApi.pipelineSummary(signal),
    refetchInterval: 3_000,
  });
  const params = { status: filter, limit: PAGE, cursor };
  const events = useQuery({
    queryKey: queryKeys.observability.events(params),
    queryFn: ({ signal }) => observabilityApi.events(params, signal),
    refetchInterval: cursor ? false : 5_000,
  });

  const disabled = isApiError(summary.error) && summary.error.status === 404;
  const s = summary.data;
  const dlq = s?.consumers.flatMap((c) => c.queues).filter((q) => q.role === "dead_letter").reduce((n, q) => n + q.ready, 0) ?? 0;
  const queued = s?.consumers.flatMap((c) => c.queues).filter((q) => q.role !== "dead_letter").reduce((n, q) => n + q.ready, 0) ?? 0;
  const v = s ? verdict(s) : null;

  return (
    <div className="space-y-6">
      <PageHeader
        title="Event Pipeline"
        description="Every committed money movement becomes an event: outbox → publisher → RabbitMQ → consumers. Select one to follow it."
      />
      {summary.isLoading ? (
        <LoadingRows rows={3} />
      ) : disabled ? (
        <Card>
          <CardContent>
            <EmptyState icon={<Workflow className="size-5" />} title="The event pipeline view is disabled on this backend" description="It is part of the development-only surface (ENABLE_DEV_ENDPOINTS=true)." />
          </CardContent>
        </Card>
      ) : !s ? (
        <Card>
          <CardContent>
            <ErrorState error={summary.error} onRetry={() => void summary.refetch()} />
          </CardContent>
        </Card>
      ) : (
        <>
          {v && (
            <StatusSummary tone={v.tone} title={v.title} data-testid="pipeline-verdict">
              {v.body}
            </StatusSummary>
          )}
          <div className="flex flex-col gap-2 md:flex-row md:items-center" aria-label="Pipeline stages">
            <Stage icon={<Inbox />} title="Outbox: waiting" value={s.outbox_pending + s.outbox_processing} sub={s.oldest_unpublished_age_seconds !== null ? `oldest ${ago(s.oldest_unpublished_age_seconds)}` : "none waiting"} tone={s.awaiting_retry ? "warning" : undefined} />
            <Arrow />
            <Stage icon={<Send />} title="Published" value={s.outbox_published} sub={`${s.published_last_5m} in the last 5 min`} />
            <Arrow />
            <Stage icon={<ListOrdered />} title="RabbitMQ: queued" value={s.consumers.some((c) => c.reported_at) ? queued : "?"} sub={dlq ? `${dlq} dead-lettered` : "DLQ empty"} tone={dlq ? "destructive" : undefined} />
            <Arrow />
            <Stage
              icon={<Cog />}
              title="Consumed"
              value={s.consumers.map((c) => c.events_processed).join(" / ") || 0}
              sub={s.consumers.map((c) => c.consumer).join(" / ") || "no consumers"}
            />
          </div>

          <Disclosure variant="boxed" className="bg-card" title="Retries, queues and delivery details">
            <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <KeyValue label="Publish lag p50 / p95">{s.publish_lag_ms_p50 !== null ? `${Math.round(s.publish_lag_ms_p50)} / ${Math.round(s.publish_lag_ms_p95 ?? 0)} ms` : "—"}</KeyValue>
              <KeyValue label="Awaiting retry">{s.awaiting_retry}</KeyValue>
              <KeyValue label="Needed >1 publish attempt">{s.republished_events}</KeyValue>
              <KeyValue label="Last publisher error">{s.publisher_last_error ? `${s.publisher_last_error} (${ago(secondsSince(s.publisher_last_error_at))})` : "none"}</KeyValue>
            </dl>
            <div className="mt-4 space-y-3">
              {s.consumers.map((c) => (
                <div key={c.consumer} className="rounded-md border p-3 text-sm">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="font-medium">{c.consumer}</span>
                    <span className="text-xs text-muted-foreground">
                      {c.reported_at ? `worker report ${ago(secondsSince(c.reported_at))}${c.report_stale ? " (stale)" : ""}` : "no worker report"}
                    </span>
                  </div>
                  <dl className="mt-2 grid grid-cols-2 gap-3 sm:grid-cols-4">
                    <KeyValue label="Receipts (all time)">{c.events_processed}</KeyValue>
                    <KeyValue label="Applied on a redelivery">{c.effects_after_redelivery ?? "—"}</KeyValue>
                    {Object.entries(c.since_start ?? {}).map(([k, n]) => (
                      <KeyValue key={k} label={`${k.replace("_", " ")} (since start)`}>
                        {n}
                      </KeyValue>
                    ))}
                  </dl>
                  {c.queues.length > 0 && (
                    <p className="mt-2 text-xs text-muted-foreground">
                      Queues: {c.queues.map((q) => `${q.queue.split(".").slice(-2).join(".")} ${q.ready}`).join(" · ")}
                    </p>
                  )}
                </div>
              ))}
            </div>
            <p className="mt-3 text-xs text-muted-foreground">Counts come from PostgreSQL; queue depths are reported by the consumer workers (the API never connects to RabbitMQ).</p>
          </Disclosure>
        </>
      )}

      {!disabled && (
        <Card className="gap-0 py-0">
          <Tabs<EventStatusFilter>
            idPrefix="events"
            label="Event filter"
            value={filter}
            onChange={(f) => {
              setFilter(f);
              setCursors([]);
            }}
            className="px-2 pt-1"
            items={[
              { id: "all", label: "Recent" },
              { id: "unpublished", label: "Waiting" },
              { id: "retrying", label: "Retrying" },
              { id: "published", label: "Published" },
            ]}
          />
          <CardContent className="p-0">
            {events.isLoading ? (
              <LoadingRows rows={5} className="p-4" />
            ) : events.error ? (
              <ErrorState error={events.error} onRetry={() => void events.refetch()} compact />
            ) : (events.data?.items.length ?? 0) === 0 ? (
              <EmptyState icon={<Inbox className="size-5" />} title="No events here" description="Make a transfer in the Transaction Playground to create one." />
            ) : (
              <div className="overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Time</TableHead>
                      <TableHead>Event</TableHead>
                      <TableHead className="text-right">Amount</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead className="hidden sm:table-cell">Consumed by</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {events.data!.items.map((e) => (
                      <TableRow key={e.id} className="cursor-pointer" onClick={() => setSelected(e)} data-event={e.id}>
                        <TableCell className="whitespace-nowrap tabular-nums">{formatTime(new Date(e.created_at))}</TableCell>
                        <TableCell>
                          <button type="button" className="text-left font-medium outline-none hover:underline focus-visible:underline" onClick={() => setSelected(e)}>
                            {e.event_type}
                          </button>
                        </TableCell>
                        <TableCell className="text-right">{e.amount_minor !== null ? <Money minor={e.amount_minor} /> : "—"}</TableCell>
                        <TableCell>
                          <StatusCell e={e} />
                        </TableCell>
                        <TableCell className="hidden text-xs text-muted-foreground sm:table-cell">{e.consumers.map((c) => c.consumer).join(", ") || "—"}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}
            <div className="flex items-center justify-between gap-2 border-t px-4 py-2">
              <Button size="sm" variant="ghost" disabled={cursors.length === 0} onClick={() => setCursors((c) => c.slice(0, -1))}>
                Newer
              </Button>
              <Button size="sm" variant="ghost" disabled={!events.data?.next_cursor} onClick={() => events.data?.next_cursor && setCursors((c) => [...c, events.data!.next_cursor!])}>
                Older
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      <Sheet open={!!selected} onOpenChange={(open) => !open && setSelected(null)}>
        <SheetContent>
          <SheetHeader>
            <SheetTitle>Follow one event</SheetTitle>
            <SheetDescription>Transfer → outbox → publisher → RabbitMQ → consumers, from persisted state.</SheetDescription>
          </SheetHeader>
          {selected && (
            <div className="space-y-5 p-5">
              <dl className="grid grid-cols-2 gap-4">
                <KeyValue label="Event id" className="col-span-2">
                  <IdText id={selected.id} />
                </KeyValue>
                <KeyValue label="Type">{`${selected.event_type} v${selected.event_version}`}</KeyValue>
                <KeyValue label="Publish attempts">{selected.attempts}</KeyValue>
                <KeyValue label="Correlation id" className="col-span-2">
                  {selected.correlation_id ? <IdText id={selected.correlation_id} /> : "—"}
                </KeyValue>
                {selected.trace_id && (
                  <KeyValue label="Trace id" className="col-span-2">
                    <IdText id={selected.trace_id} />
                  </KeyValue>
                )}
              </dl>
              {selected.transfer_id ? (
                <LifecycleExplorer transferId={selected.transfer_id} />
              ) : (
                <p className="text-sm text-muted-foreground">This event names no transfer.</p>
              )}
            </div>
          )}
        </SheetContent>
      </Sheet>
    </div>
  );
}
