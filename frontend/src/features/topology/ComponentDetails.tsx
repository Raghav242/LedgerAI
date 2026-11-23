import { X } from "lucide-react";
import { Disclosure } from "@/components/common/Disclosure";
import { KeyValue } from "@/components/common/display";
import { Button } from "@/components/ui/button";
import { formatDateTime } from "@/lib/format";
import { StatusLabel } from "@/features/observability/health";
import { EVIDENCE_LABEL, STATUS_META, ago, secondsSince } from "@/features/observability/healthMeta";
import type { ComponentHealth } from "@/types/api";
import { infoFor } from "./componentInfo";

function metricValue(value: unknown, unit?: string | null): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}T/.test(value)) return ago(secondsSince(value));
  return `${String(value)}${unit ? ` ${unit}` : ""}`;
}

/** Live status first (what, when, how we know), then the static explanation behind a disclosure. */
export function ComponentDetails({ component, onClose }: { component: ComponentHealth; onClose?: () => void }) {
  const info = infoFor(component.id, component.kind);
  const observedAgo = component.observed_at ? ago(secondsSince(component.observed_at)) : null;
  return (
    <div className="space-y-4" data-testid="component-details" aria-live="polite">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="truncate font-semibold">{component.name}</h3>
          <p className="text-xs text-muted-foreground">{info.short}</p>
        </div>
        <div className="flex items-center gap-1">
          <StatusLabel status={component.status} />
          {onClose && (
            <Button variant="ghost" size="icon" className="size-7" onClick={onClose} aria-label="Close details">
              <X />
            </Button>
          )}
        </div>
      </div>
      <p className="text-sm">{component.message}</p>
      <dl className="grid grid-cols-2 gap-3">
        <KeyValue label="Evidence">
          <span title={STATUS_META[component.status].hint}>{EVIDENCE_LABEL[component.evidence]}</span>
        </KeyValue>
        <KeyValue label="Observed">{observedAgo ?? "not observed"}</KeyValue>
        <KeyValue label="Last OK">{component.last_ok_at ? ago(secondsSince(component.last_ok_at)) : "not seen"}</KeyValue>
        {component.latency_ms !== null && component.latency_ms !== undefined && <KeyValue label="Probe latency">{component.latency_ms} ms</KeyValue>}
      </dl>
      <p className="text-xs text-muted-foreground">Source: {component.source}</p>
      {component.last_error && (
        <div className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs">
          <div className="font-medium text-destructive">Most recent error</div>
          <div className="break-words">{component.last_error}</div>
          {component.last_error_at && <div className="text-muted-foreground">{formatDateTime(component.last_error_at)}</div>}
        </div>
      )}
      {component.metrics && component.metrics.length > 0 && (
        <Disclosure title="Metrics" meta={String(component.metrics.length)} defaultOpen>
          <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
            {component.metrics.map((m) => (
              <KeyValue key={m.key} label={m.label}>
                <span className="tabular-nums">{metricValue(m.value, m.unit)}</span>
              </KeyValue>
            ))}
          </dl>
        </Disclosure>
      )}
      {info.responsibility && (
        <Disclosure title="Responsibility & design decisions">
          <p className="text-sm">{info.responsibility}</p>
          <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-muted-foreground">
            {info.decisions.map((d) => (
              <li key={d}>{d}</li>
            ))}
          </ul>
        </Disclosure>
      )}
    </div>
  );
}
