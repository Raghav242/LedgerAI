import { Loader2, RefreshCw, ShieldAlert, ShieldCheck } from "lucide-react";
import { DataSourceTag, IdText, Money } from "@/components/common/display";
import { ErrorState, LoadingRows } from "@/components/common/states";
import { InvariantCheckList } from "@/components/lab/InvariantCheckList";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useReconciliation } from "@/hooks/queries";
import { formatCount, formatTime } from "@/lib/format";
import type { Reconciliation } from "@/types/api";
import { reconciliationInvariants } from "./reconciliationInvariants";

export function ReconciliationPanel() {
  const query = useReconciliation();
  const data = query.data;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Reconciliation</CardTitle>
        <CardDescription>
          GET /api/v1/reconciliation · one REPEATABLE READ snapshot, read-only
          {query.dataUpdatedAt > 0 && <> · last run {formatTime(query.dataUpdatedAt)}</>}
        </CardDescription>
        <CardAction className="flex items-center gap-2">
          <DataSourceTag kind="live" />
          <Button variant="outline" size="sm" onClick={() => void query.refetch()} disabled={query.isFetching}>
            {query.isFetching ? <Loader2 className="animate-spin" /> : <RefreshCw />}
            Run again
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent className="space-y-5">
        {query.isLoading ? (
          <LoadingRows rows={4} />
        ) : query.error && !data ? (
          <ErrorState error={query.error} onRetry={() => void query.refetch()} retrying={query.isFetching} />
        ) : data ? (
          <>
            {query.error && (
              <ErrorState error={query.error} compact className="rounded-lg border border-destructive/30" />
            )}
            <ReconciliationVerdict data={data} stale={!!query.error} />
            <InvariantCheckList items={reconciliationInvariants(data)} />
            {data.balance_mismatches.length > 0 && (
              <div className="space-y-2">
                <h3 className="text-sm font-semibold text-destructive">Balance mismatches</h3>
                <div className="rounded-lg border">
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
          </>
        ) : null}
      </CardContent>
    </Card>
  );
}

function ReconciliationVerdict({ data, stale }: { data: Reconciliation; stale: boolean }) {
  // The verdict is the backend's `ok` field, verbatim.
  return data.ok ? (
    <div className="flex items-start gap-3 rounded-lg border border-success/30 bg-success/5 p-4" data-testid="reconciliation-verdict" data-ok="true">
      <ShieldCheck className="mt-0.5 size-5 text-success" />
      <div>
        <div className="font-semibold text-success">Reconciliation passed{stale && " (last successful run)"}</div>
        <p className="text-sm text-muted-foreground">
          {formatCount(data.accounts_checked)} accounts and {formatCount(data.ledger_entries_checked)} ledger entries checked. Every balance is
          explained by the ledger.
        </p>
      </div>
    </div>
  ) : (
    <div className="flex items-start gap-3 rounded-lg border border-destructive/40 bg-destructive/5 p-4" data-testid="reconciliation-verdict" data-ok="false">
      <ShieldAlert className="mt-0.5 size-5 text-destructive" />
      <div>
        <div className="font-semibold text-destructive">Reconciliation FAILED</div>
        <p className="text-sm text-muted-foreground">The backend reported at least one violated invariant. See the details below.</p>
      </div>
    </div>
  );
}
