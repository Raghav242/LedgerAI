import { ArrowLeftRight, ArrowRight, Banknote, BarChart3, ShieldAlert, ShieldCheck, Users, Wifi, WifiOff } from "lucide-react";
import { lazy, Suspense, type ReactNode } from "react";
import { Link, useNavigate } from "react-router";
import { DataSourceTag, PageHeader } from "@/components/common/display";
import { StatCard } from "@/components/common/StatCard";
import { EmptyState, ErrorState, LoadingRows } from "@/components/common/states";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { TransferTable } from "@/features/history/TransferTable";
import { useReadiness, useReconciliation, useSummary, useTransfers } from "@/hooks/queries";
import { useAccountDirectory } from "@/hooks/useAccountDirectory";
import { formatCount, formatTime } from "@/lib/format";
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
        description="A live snapshot of the virtual transaction system. Every number on this page is read from the running backend; aggregates are computed in PostgreSQL over one consistent snapshot."
        actions={<DataSourceTag kind="live" />}
      />

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label="Virtual accounts"
          icon={<Users />}
          loading={summary.isLoading}
          error={!!summary.error}
          value={summary.data && formatCount(summary.data.merchant_account_count)}
          hint="Merchant accounts (excludes the system funding account)"
        />
        <StatCard
          label="Completed transfers"
          icon={<ArrowLeftRight />}
          loading={summary.isLoading}
          error={!!summary.error}
          value={transfers && formatCount(transfers.count)}
          hint="Merchant-to-merchant; rejected attempts are not stored"
        />
        <StatCard
          label="Transfer volume"
          icon={<BarChart3 />}
          loading={summary.isLoading}
          error={!!summary.error}
          value={transfers && formatMinor(transfers.volume_minor)}
          hint="Sum of all completed merchant transfers (virtual USD)"
        />
        <StatCard
          label="Simulated money held"
          icon={<Banknote />}
          loading={summary.isLoading}
          error={!!summary.error}
          value={summary.data && formatMinor(summary.data.merchant_balance_total_minor)}
          hint={funding ? `Issued via ${formatCount(funding.count)} dev fundings (${formatMinor(funding.volume_minor)})` : "Across all merchant accounts"}
        />
      </div>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <HealthCard
          title="Ledger reconciliation"
          to="/ledger"
          loading={reconciliation.isLoading}
          error={reconciliation.error}
          ok={reconciliation.data?.ok}
          okText="Passed: every balance matches the ledger"
          failText="FAILED: the backend reported a violated invariant"
          detail={
            reconciliation.data &&
            `${formatCount(reconciliation.data.accounts_checked)} accounts · ${formatCount(reconciliation.data.ledger_entries_checked)} entries · checked ${formatTime(reconciliation.dataUpdatedAt)}`
          }
          okIcon={<ShieldCheck className="size-5 text-success" />}
          failIcon={<ShieldAlert className="size-5 text-destructive" />}
        />
        <HealthCard
          title="API connectivity"
          loading={readiness.isLoading}
          error={readiness.error}
          ok={readiness.data ? readiness.data.report.status === "ok" : undefined}
          okText="Ready: PostgreSQL and Redis reachable"
          failText="Degraded: a dependency is unavailable"
          detail={
            readiness.data &&
            `database ${readiness.data.report.database ?? "n/a"} · redis ${readiness.data.report.redis ?? "n/a"} · answered by ${readiness.data.report.replica ?? "?"} via NGINX`
          }
          okIcon={<Wifi className="size-5 text-success" />}
          failIcon={<WifiOff className="size-5 text-warning" />}
        />
      </div>

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

function HealthCard({
  title,
  to,
  loading,
  error,
  ok,
  okText,
  failText,
  detail,
  okIcon,
  failIcon,
}: {
  title: string;
  to?: string;
  loading: boolean;
  error: unknown;
  ok: boolean | undefined;
  okText: string;
  failText: string;
  detail?: string | false | null;
  okIcon: ReactNode;
  failIcon: ReactNode;
}) {
  return (
    <Card className="gap-3 py-4">
      <CardHeader className="px-4">
        <CardTitle>{title}</CardTitle>
        {to && (
          <CardAction>
            <Button variant="ghost" size="sm" className="h-7" asChild>
              <Link to={to}>
                Details <ArrowRight />
              </Link>
            </Button>
          </CardAction>
        )}
      </CardHeader>
      <CardContent className="px-4">
        {loading ? (
          <LoadingRows rows={1} />
        ) : error && ok === undefined ? (
          <ErrorState error={error} compact className="items-start py-0 text-left" />
        ) : (
          <div className="flex items-start gap-3">
            {ok ? okIcon : failIcon}
            <div className="min-w-0">
              <div className="text-sm font-medium">{ok ? okText : failText}</div>
              {detail && <div className="truncate text-xs text-muted-foreground">{detail}</div>}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

