import { Network, RefreshCw } from "lucide-react";
import { useState } from "react";
import { isApiError } from "@/api/client";
import { Disclosure } from "@/components/common/Disclosure";
import { PageHeader } from "@/components/common/display";
import { StatusSummary } from "@/components/common/StatusSummary";
import { EmptyState, ErrorState, LoadingRows } from "@/components/common/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { ago } from "@/features/observability/healthMeta";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import type { ComponentHealth } from "@/types/api";
import { ComponentDetails } from "./ComponentDetails";
import { RequestFlowPresets } from "./RequestFlowPresets";
import { TopologyDiagram, TopologyList } from "./TopologyDiagram";
import { useLiveTopology } from "./useLiveTopology";

const PROBLEM = new Set(["degraded", "unhealthy", "stale", "unknown"]);

function verdict(components: ComponentHealth[], stale: boolean) {
  const considered = components.filter((c) => c.status !== "not_configured");
  const problems = considered.filter((c) => PROBLEM.has(c.status));
  if (stale) return { tone: "muted" as const, title: "Live status is out of date", problems };
  if (problems.length === 0) return { tone: "success" as const, title: `All ${considered.length} components healthy`, problems };
  const bad = problems.some((c) => c.status === "unhealthy");
  return {
    tone: bad ? ("destructive" as const) : ("warning" as const),
    title: `${problems.length} of ${considered.length} components need attention`,
    problems,
  };
}

/** Live architecture: each component's real status, click one for details. */
export function TopologyPage() {
  const live = useLiveTopology();
  const wide = useMediaQuery("(min-width: 768px)");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = live.components.find((c) => c.id === selectedId) ?? null;
  const disabled = isApiError(live.error) && live.error.status === 404 && !live.snapshot;

  const updated =
    live.snapshotAgeMs === null ? null : `Updated ${ago(live.snapshotAgeMs / 1000)} by ${live.snapshot?.observed_by ?? "?"}`;

  return (
    <div className="space-y-6">
      <PageHeader
        title="System Topology"
        description="The running system, component by component. Select one to see how its status was observed."
        actions={
          updated && (
            <Badge variant={live.stale ? "outline" : "success"} title="Status is polled every 5 s">
              {live.stale ? "Stale" : "Live"} · {updated}
            </Badge>
          )
        }
      />

      {live.isLoading ? (
        <LoadingRows rows={5} />
      ) : disabled ? (
        <Card>
          <CardContent>
            <EmptyState
              icon={<Network className="size-5" />}
              title="Live topology is disabled on this backend"
              description="It is part of the development-only surface. Set ENABLE_DEV_ENDPOINTS=true and restart the stack."
            />
          </CardContent>
        </Card>
      ) : !live.snapshot ? (
        <Card>
          <CardContent>
            <ErrorState error={live.error} onRetry={live.refetch} />
          </CardContent>
        </Card>
      ) : (
        <>
          {(() => {
            const v = verdict(live.components, live.stale);
            return (
              <StatusSummary
                tone={v.tone}
                title={v.title}
                data-testid="topology-verdict"
                actions={
                  live.stale ? (
                    <Button size="sm" variant="outline" onClick={live.refetch}>
                      <RefreshCw /> Retry
                    </Button>
                  ) : undefined
                }
              >
                {live.stale
                  ? "The backend has not answered recently, so the last known statuses are shown as stale, not as current."
                  : v.problems.length > 0
                    ? v.problems.map((c) => `${c.name}: ${c.status.replace("_", " ")}`).join(" · ")
                    : "Every status below was observed within the last few seconds."}
              </StatusSummary>
            );
          })()}

          <div className="grid grid-cols-1 items-start gap-4 2xl:grid-cols-[minmax(0,1fr)_360px]">
            <Card className="min-w-0">
              <CardContent className="p-3 sm:p-4">
                {wide ? (
                  <TopologyDiagram components={live.components} selectedId={selectedId} onSelect={setSelectedId} />
                ) : (
                  <TopologyList components={live.components} selectedId={selectedId} onSelect={setSelectedId} />
                )}
                <p className="mt-2 text-xs text-muted-foreground">
                  Healthy = observed working just now · Stale = last report too old · Unknown = no evidence. Dashed lines are asynchronous.
                </p>
              </CardContent>
            </Card>
            <Card className="min-w-0">
              <CardContent className="p-4">
                {selected ? (
                  <ComponentDetails component={selected} onClose={() => setSelectedId(null)} />
                ) : (
                  <p className="text-sm text-muted-foreground">Select a component to see its evidence, metrics, recent errors and design decisions.</p>
                )}
              </CardContent>
            </Card>
          </div>
        </>
      )}

      <Disclosure variant="boxed" className="bg-card" title="How requests travel (illustrated flows)">
        <RequestFlowPresets />
      </Disclosure>
    </div>
  );
}
