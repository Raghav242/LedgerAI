import { ChevronLeft, ChevronRight, History, RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";
import { useSearchParams } from "react-router";
import type { TransferListParams } from "@/api/transfers";
import { DataSourceTag, PageHeader } from "@/components/common/display";
import { EmptyState, ErrorState, InlineSpinner, LoadingRows } from "@/components/common/states";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { useTransfers } from "@/hooks/queries";
import { useAccountDirectory } from "@/hooks/useAccountDirectory";
import { TRANSFER_KIND_LABELS, TRANSFER_KINDS, type TransferKind } from "@/types/api";
import { TransferDetailSheet } from "./TransferDetailSheet";
import { TransferTable, type SortKey, type SortState } from "./TransferTable";

const PAGE_SIZES = [25, 50, 100] as const;

export function HistoryPage() {
  const [params, setParams] = useSearchParams();
  const directory = useAccountDirectory();
  const [sort, setSort] = useState<SortState>({ key: "created_at", dir: "desc" });

  const accountId = params.get("account") ?? "";
  const kind = (params.get("kind") ?? "") as TransferKind | "";
  const pageSize = PAGE_SIZES.find((s) => String(s) === params.get("size")) ?? 25;
  const page = Math.max(0, Number(params.get("page") ?? 0) || 0);
  const selectedTransfer = params.get("transfer");

  const listParams: TransferListParams = {
    account_id: accountId || undefined,
    kind: kind || undefined,
    limit: pageSize,
    offset: page * pageSize,
  };
  const query = useTransfers(listParams);
  const items = query.data?.items;
  const hasNext = (items?.length ?? 0) === pageSize; // the API returns no total count

  // Server order is newest first; sorting here re-orders only the current page.
  const rows = useMemo(() => {
    const list = [...(items ?? [])];
    const factor = sort.dir === "asc" ? 1 : -1;
    list.sort((a, b) =>
      sort.key === "amount_minor" ? (a.amount_minor - b.amount_minor) * factor : a.created_at.localeCompare(b.created_at) * factor,
    );
    return list;
  }, [items, sort]);

  function update(changes: Record<string, string | null>, resetPage = true) {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(changes)) {
      if (v === null || v === "") next.delete(k);
      else next.set(k, v);
    }
    if (resetPage) next.delete("page");
    setParams(next, { replace: true });
  }

  function toggleSort(key: SortKey) {
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "desc" }));
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Transaction History"
        description="Every committed money movement. Records are immutable; rejected attempts are not stored."
        actions={
          <>
            <DataSourceTag kind="live" />
            <Button variant="outline" size="sm" onClick={() => void query.refetch()} disabled={query.isFetching}>
              <RefreshCw className={query.isFetching ? "animate-spin" : undefined} /> Refresh
            </Button>
          </>
        }
      />

      <Card className="gap-0 py-0">
        <div className="flex flex-wrap items-end gap-3 border-b p-4">
          <div className="grid w-full gap-1.5 sm:w-64">
            <Label htmlFor="filter-account">Account</Label>
            <NativeSelect id="filter-account" value={accountId} onChange={(e) => update({ account: e.target.value })}>
              <option value="">All accounts</option>
              {directory.accounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </NativeSelect>
          </div>
          <div className="grid w-full gap-1.5 sm:w-52">
            <Label htmlFor="filter-kind">Type</Label>
            <NativeSelect id="filter-kind" value={kind} onChange={(e) => update({ kind: e.target.value })}>
              <option value="">All types</option>
              {TRANSFER_KINDS.map((k) => (
                <option key={k} value={k}>
                  {TRANSFER_KIND_LABELS[k]}
                </option>
              ))}
            </NativeSelect>
          </div>
          <div className="grid w-full gap-1.5 sm:w-28">
            <Label htmlFor="filter-size">Page size</Label>
            <NativeSelect id="filter-size" value={pageSize} onChange={(e) => update({ size: e.target.value })}>
              {PAGE_SIZES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </NativeSelect>
          </div>
          {query.isFetching && !query.isLoading && <InlineSpinner label="Updating" />}
        </div>

        <CardContent className="px-0">
          {query.isLoading ? (
            <LoadingRows rows={8} className="p-4" />
          ) : query.error ? (
            <ErrorState error={query.error} onRetry={() => void query.refetch()} retrying={query.isFetching} />
          ) : rows.length === 0 ? (
            <EmptyState
              icon={<History className="size-5" />}
              title={page > 0 ? "No more transfers" : "No transfers match"}
              description={
                accountId || kind
                  ? "Try clearing the filters."
                  : "Send a transfer from the Transaction Playground and it will appear here."
              }
            />
          ) : (
            <TransferTable rows={rows} sort={sort} onSort={toggleSort} nameOf={directory.nameOf} onOpen={(id) => update({ transfer: id }, false)} />
          )}
        </CardContent>

        <div className="flex flex-wrap items-center justify-between gap-3 border-t px-4 py-3 text-xs text-muted-foreground">
          <span>
            Page {page + 1} · rows {page * pageSize + (rows.length ? 1 : 0)}–{page * pageSize + rows.length} · newest first from the server; column
            sorting applies to this page
          </span>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" disabled={page === 0 || query.isFetching} onClick={() => update({ page: String(page - 1) }, false)}>
              <ChevronLeft /> Previous
            </Button>
            <Button variant="outline" size="sm" disabled={!hasNext || query.isFetching} onClick={() => update({ page: String(page + 1) }, false)}>
              Next <ChevronRight />
            </Button>
          </div>
        </div>
      </Card>

      <TransferDetailSheet transferId={selectedTransfer} onClose={() => update({ transfer: null }, false)} nameOf={directory.nameOf} />
    </div>
  );
}
