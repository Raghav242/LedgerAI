import { Loader2, RefreshCw, ShieldAlert, ShieldCheck } from "lucide-react";
import type { ReactNode } from "react";
import { IdText, Money } from "@/components/common/display";
import { Disclosure } from "@/components/common/Disclosure";
import { ErrorState, LoadingRows } from "@/components/common/states";
import { StatusSummary } from "@/components/common/StatusSummary";
import { InvariantCheckList } from "@/components/lab/InvariantCheckList";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useReconciliation } from "@/hooks/queries";
import { formatCount, formatTime } from "@/lib/format";
import type { Reconciliation } from "@/types/api";
import { reconciliationInvariants } from "./reconciliationInvariants";

/**
 * Reconciliation leads with the backend's verdict (its `ok` field, verbatim). Invariants and
 * discrepancies are one click away, and open by themselves when something is wrong.
 */
export function ReconciliationPanel() {
  const query = useReconciliation();
  const data = query.data;
  const rerun = (
    <Button variant="outline" size="sm" onClick={() => void query.refetch()} disabled={query.isFetching}>
      {query.isFetching ? <Loader2 className="animate-spin" /> : <RefreshCw />}
      Run again
    </Button>
  );

  return (
    <Card className="gap-4 py-4" role="region" aria-label="Reconciliation">
      <CardContent className="space-y-4 px-4">
        {query.isLoading ? (
          <LoadingRows rows={3} />
        ) : query.error && !data ? (
          <ErrorState error={query.error} onRetry={() => void query.refetch()} retrying={query.isFetching} />
        ) : data ? (
          <>
            {query.error && <ErrorState error={query.error} compact className="rounded-lg border border-destructive/30" />}
            <Verdict data={data} stale={!!query.error} updatedAt={query.dataUpdatedAt} actions={rerun} />
            <Disclosure
              variant="boxed"
              title="Invariants"
              meta={`${reconciliationInvariants(data).filter((i) => i.status === "pass").length} of 4 hold`}
              defaultOpen={!data.ok}
            >
              <div className="space-y-3">
                <InvariantCheckList items={reconciliationInvariants(data)} />
                <p className="text-xs text-muted-foreground">
                  GET /api/v1/reconciliation · one REPEATABLE READ snapshot, read-only. Balances are a cache; the immutable ledger is the source of
                  truth.
                </p>
              </div>
            </Disclosure>
            {(data.balance_mismatches.length > 0 || data.unbalanced_transfer_ids.length > 0) && (
              <Disclosure
                variant="boxed"
                title={<span className="text-destructive">Discrepancies</span>}
                meta={`${data.balance_mismatches.length + data.unbalanced_transfer_ids.length}`}
                defaultOpen
              >
                <Discrepancies data={data} />
              </Disclosure>
            )}
          </>
        ) : null}
      </CardContent>
    </Card>
  );
}

function Verdict({ data, stale, updatedAt, actions }: { data: Reconciliation; stale: boolean; updatedAt: number; actions: ReactNode }) {
  const detail = `${formatCount(data.accounts_checked)} accounts · ${formatCount(data.ledger_entries_checked)} ledger entries${updatedAt ? ` · checked ${formatTime(updatedAt)}` : ""}`;
  return data.ok ? (
    <StatusSummary
      tone="success"
      icon={<ShieldCheck />}
      title={`Ledger balanced — all checks passed${stale ? " (last successful run)" : ""}`}
      actions={actions}
      data-testid="reconciliation-verdict"
      data-ok="true"
    >
      {detail}
    </StatusSummary>
  ) : (
    <StatusSummary
      tone="destructive"
      icon={<ShieldAlert />}
      title="Reconciliation failed — discrepancies detected"
      actions={actions}
      data-testid="reconciliation-verdict"
      data-ok="false"
    >
      {detail}
    </StatusSummary>
  );
}

function Discrepancies({ data }: { data: Reconciliation }) {
  return (
    <div className="space-y-4">
      {data.balance_mismatches.length > 0 && (
        <div className="space-y-2">
          <h3 className="text-sm font-semibold text-destructive">Balance mismatches</h3>
          <div className="overflow-x-auto rounded-lg border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Account</TableHead>
                  <TableHead className="text-right">Stored balance</TableHead>
                  <TableHead className="text-right">Ledger balance</TableHead>
                  <TableHead className="text-right">Difference</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.balance_mismatches.map((m) => (
                  <TableRow key={m.account_id}>
                    <TableCell>
                      <IdText id={m.account_id} />
                    </TableCell>
                    <TableCell className="text-right">
                      <Money minor={m.balance_minor} />
                    </TableCell>
                    <TableCell className="text-right">
                      <Money minor={m.ledger_balance_minor} />
                    </TableCell>
                    <TableCell className="text-right">
                      <Money minor={m.balance_minor - m.ledger_balance_minor} signed />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </div>
      )}
      {data.unbalanced_transfer_ids.length > 0 && (
        <div className="space-y-2">
          <h3 className="text-sm font-semibold text-destructive">Unbalanced transfers</h3>
          <ul className="flex flex-wrap gap-2">
            {data.unbalanced_transfer_ids.map((id) => (
              <li key={id}>
                <IdText id={id} />
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
