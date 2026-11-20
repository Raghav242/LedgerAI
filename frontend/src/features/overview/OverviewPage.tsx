import { ArrowLeftRight, ArrowRight, Banknote, BarChart3, ShieldAlert, ShieldCheck, Users, Wifi, WifiOff } from "lucide-react";
import { lazy, Suspense, type ReactNode } from "react";
import { Link, useNavigate } from "react-router";
import { DataSourceTag, PageHeader } from "@/components/common/display";
import { StatCard } from "@/components/common/StatCard";
import { Disclosure } from "@/components/common/Disclosure";
import { EmptyState, ErrorState, InlineSpinner, LoadingRows } from "@/components/common/states";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { TransferTable } from "@/features/history/TransferTable";
import { useReadiness, useReconciliation, useSummary, useTransfers } from "@/hooks/queries";
import { useAccountDirectory } from "@/hooks/useAccountDirectory";
import { formatCount, formatTime } from "@/lib/format";
import { describeError } from "@/lib/errors";
import { formatMinor } from "@/lib/money";

// Recharts is the largest dependency and only this chart needs it: load it on demand.
const ActivityChart = lazy(() => import("./ActivityChart").then((m) => ({ default: m.ActivityChart })));

const ACTIVITY_DAYS = 14;
const RECENT_LIMIT = 8;

export function OverviewPage() {
  const summary = useSummary(ACTIVITY_DAYS);
  const recent = useTransfers({ limit: RECENT_LIMIT, offset: 0 });
  const reconciliation = useReconciliation();
  const readiness = useReadiness();
  const directory = useAccountDirectory();
  const navigate = useNavigate();

  const kinds = summary.data?.transfers_by_kind ?? [];
  const transfers = kinds.find((k) => k.kind === "transfer");
  const funding = kinds.find((k) => k.kind === "dev_funding");
  const activityTotal = summary.data?.daily_transfer_activity.reduce((s, d) => s + d.count, 0) ?? 0;

  return (
    <div className="space-y-6">
      <PageHeader
        title="Overview"
        description="A live snapshot of the system. Every number comes from the running backend."
        actions={<DataSourceTag kind="live" />}
      />

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label="Virtual accounts"
          icon={<Users />}
          loading={summary.isLoading}
          error={!!summary.error}
          value={summary.data && formatCount(summary.data.merchant_account_count)}
          hint="Merchant accounts"
        />
        <StatCard
          label="Completed transfers"
          icon={<ArrowLeftRight />}
          loading={summary.isLoading}
          error={!!summary.error}
          value={transfers && formatCount(transfers.count)}
          hint="Merchant to merchant"
        />
        <StatCard
          label="Transfer volume"
          icon={<BarChart3 />}
          loading={summary.isLoading}
          error={!!summary.error}
          value={transfers && formatMinor(transfers.volume_minor)}
          hint="Completed transfers, virtual USD"
        />
        <StatCard
          label="Simulated money held"
          icon={<Banknote />}
          loading={summary.isLoading}
          error={!!summary.error}
          value={summary.data && formatMinor(summary.data.merchant_balance_total_minor)}
          hint={funding ? `From ${formatCount(funding.count)} simulated fundings` : "Across merchant accounts"}
        />
      </div>

      <Card className="gap-2 py-3" role="region" aria-label="System status">
        <CardContent className="space-y-1 px-4">
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
            <StatusItem
              loading={reconciliation.isLoading}
              error={reconciliation.error}
              ok={reconciliation.data?.ok}
              okText="Ledger balanced"
              failText="Reconciliation failed"
              okIcon={<ShieldCheck />}
              failIcon={<ShieldAlert />}
              to="/ledger"
            />
            <StatusItem
              loading={readiness.isLoading}
              error={readiness.error}
              ok={readiness.data ? readiness.data.report.status === "ok" : undefined}
              okText="API ready"
              failText="API degraded"
              okIcon={<Wifi />}
              failIcon={<WifiOff />}
            />
          </div>
          <Disclosure title="Status details">
            <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
              <div>
                <dt className="text-xs text-muted-foreground">Reconciliation</dt>
                <dd>
                  {reconciliation.data
                    ? `${reconciliation.data.ok ? "Passed: every balance matches the ledger" : "FAILED: the backend reported a violated invariant"} · ${formatCount(reconciliation.data.accounts_checked)} accounts · ${formatCount(reconciliation.data.ledger_entries_checked)} entries · checked ${formatTime(reconciliation.dataUpdatedAt)}`
                    : "—"}
                </dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">Dependencies</dt>
                <dd>
                  {readiness.data
                    ? `${readiness.data.report.status === "ok" ? "Ready: PostgreSQL and Redis reachable" : "Degraded: a dependency is unavailable"} · database ${readiness.data.report.database ?? "n/a"} · redis ${readiness.data.report.redis ?? "n/a"} · answered by ${readiness.data.report.replica ?? "?"} via NGINX`
                    : "—"}
                </dd>
              </div>
            </dl>
          </Disclosure>
        </CardContent>
      </Card>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-5">
        <Card className="xl:col-span-2">
          <CardHeader>
            <CardTitle>Transfers per day</CardTitle>
            <CardDescription>
              Last {ACTIVITY_DAYS} days (UTC) · {formatCount(activityTotal)} merchant transfers
            </CardDescription>
          </CardHeader>
          <CardContent>
            {summary.isLoading ? (
              <LoadingRows rows={5} />
            ) : summary.error ? (
              <ErrorState error={summary.error} onRetry={() => void summary.refetch()} compact />
            ) : activityTotal === 0 ? (
              <EmptyState title="No transfers in this window" description="Activity appears here as soon as transfers are sent." className="py-12" />
            ) : (
              <Suspense fallback={<LoadingRows rows={5} />}>
                <ActivityChart data={summary.data!.daily_transfer_activity} />
              </Suspense>
            )}
          </CardContent>
        </Card>

        <Card className="gap-3 pb-0 xl:col-span-3">
          <CardHeader>
            <CardTitle>Recent transactions</CardTitle>
            <CardDescription>The {RECENT_LIMIT} newest committed movements</CardDescription>
            <CardAction>
              <Button variant="ghost" size="sm" asChild>
                <Link to="/history">
                  View all <ArrowRight />
                </Link>
              </Button>
            </CardAction>
          </CardHeader>
          <CardContent className="px-0">
            {recent.isLoading ? (
              <LoadingRows rows={5} className="px-4 pb-4" />
            ) : recent.error ? (
              <ErrorState error={recent.error} onRetry={() => void recent.refetch()} compact />
            ) : (recent.data?.items.length ?? 0) === 0 ? (
              <EmptyState
                title="No transactions yet"
                description="Create and fund accounts in the Transaction Playground."
                action={
                  <Button size="sm" asChild>
                    <Link to="/playground">Open playground</Link>
                  </Button>
                }
              />
            ) : (
              <TransferTable compact rows={recent.data!.items} nameOf={directory.nameOf} onOpen={(id) => navigate(`/history?transfer=${id}`)} />
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

function StatusItem({
  loading,
  error,
  ok,
  okText,
  failText,
  okIcon,
  failIcon,
  to,
}: {
  loading: boolean;
  error: unknown;
  ok: boolean | undefined;
  okText: string;
  failText: string;
  okIcon: ReactNode;
  failIcon: ReactNode;
  to?: string;
}) {
  if (loading) return <InlineSpinner label="Checking…" />;
  if (ok === undefined) {
    return (
      <span className="flex items-center gap-1.5 text-sm text-destructive [&>svg]:size-4" role="status">
        <WifiOff aria-hidden /> {describeError(error).title}
      </span>
    );
  }
  const content = (
    <>
      <span className={ok ? "text-success" : "text-destructive"} aria-hidden>
        {ok ? okIcon : failIcon}
      </span>
      <span className={ok ? "" : "text-destructive"}>{ok ? okText : failText}</span>
    </>
  );
  const className = "flex items-center gap-1.5 text-sm font-medium [&_svg]:size-4";
  return to ? (
    <Link to={to} className={`${className} rounded-md outline-none hover:underline focus-visible:ring-[3px] focus-visible:ring-ring/50`}>
      {content}
    </Link>
  ) : (
    <span className={className}>{content}</span>
  );
}
