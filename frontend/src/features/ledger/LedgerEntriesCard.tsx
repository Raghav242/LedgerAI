import { BookOpen, ChevronLeft, ChevronRight } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";
import { DataSourceTag, IdText, Money } from "@/components/common/display";
import { EmptyState, ErrorState, InlineSpinner, LoadingRows } from "@/components/common/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useLedgerEntries } from "@/hooks/queries";
import { useAccountDirectory } from "@/hooks/useAccountDirectory";
import { formatDateTime } from "@/lib/format";
import { SYSTEM_FUNDING_ACCOUNT_ID } from "@/lib/ids";

const PAGE_SIZE = 20;

export function LedgerEntriesCard() {
  const directory = useAccountDirectory();
  const [accountId, setAccountId] = useState("");
  const [page, setPage] = useState(0);
  const query = useLedgerEntries({ account_id: accountId || undefined, limit: PAGE_SIZE, offset: page * PAGE_SIZE });
  const items = query.data?.items ?? [];
  const hasNext = items.length === PAGE_SIZE;

  return (
    <Card className="gap-0 pb-0">
      <CardHeader className="pb-4">
        <CardTitle>Ledger postings</CardTitle>
        <CardDescription>Newest first; each transfer is one debit and one credit.</CardDescription>
        <CardAction className="flex items-center gap-2">
          {query.isFetching && !query.isLoading && <InlineSpinner label="Updating" />}
          <DataSourceTag kind="live" />
        </CardAction>
      </CardHeader>
      <div className="flex flex-wrap items-end gap-3 border-y px-5 py-3">
        <div className="grid w-full gap-1.5 sm:w-72">
          <Label htmlFor="ledger-account">Account</Label>
          <NativeSelect
            id="ledger-account"
            value={accountId}
            onChange={(e) => {
              setAccountId(e.target.value);
              setPage(0);
            }}
          >
            <option value="">All accounts</option>
            <option value={SYSTEM_FUNDING_ACCOUNT_ID}>System funding (issuance)</option>
            {directory.accounts.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </NativeSelect>
        </div>
      </div>
      <CardContent className="px-0">
        {query.isLoading ? (
          <LoadingRows rows={6} className="p-4" />
        ) : query.error ? (
          <ErrorState error={query.error} onRetry={() => void query.refetch()} retrying={query.isFetching} />
        ) : items.length === 0 ? (
          <EmptyState icon={<BookOpen className="size-5" />} title="No ledger entries" description="Fund an account or send a transfer to create postings." />
        ) : (
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Posted</TableHead>
                <TableHead>Transfer</TableHead>
                <TableHead>Account</TableHead>
                <TableHead>Role</TableHead>
                <TableHead className="text-right">Amount</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.map((e) => (
                <TableRow key={e.id}>
                  <TableCell className="text-muted-foreground">{formatDateTime(e.created_at)}</TableCell>
                  <TableCell>
                    <Link to={`/history?transfer=${e.transfer_id}`} className="hover:underline" aria-label={`Open transfer ${e.transfer_id}`}>
                      <IdText id={e.transfer_id} copy={false} />
                    </Link>
                  </TableCell>
                  <TableCell className="max-w-[12rem] truncate">{directory.nameOf(e.account_id)}</TableCell>
                  <TableCell>
                    <Badge variant={e.role === "debit" ? "destructive" : "success"} className="capitalize">
                      {e.role}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right">
                    <Money minor={e.amount_minor} signed />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
      <div className="flex items-center justify-between gap-3 border-t px-5 py-3 text-xs text-muted-foreground">
        <span>Page {page + 1}</span>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" disabled={page === 0 || query.isFetching} onClick={() => setPage((p) => p - 1)}>
            <ChevronLeft /> Previous
          </Button>
          <Button variant="outline" size="sm" disabled={!hasNext || query.isFetching} onClick={() => setPage((p) => p + 1)}>
            Next <ChevronRight />
          </Button>
        </div>
      </div>
    </Card>
  );
}
