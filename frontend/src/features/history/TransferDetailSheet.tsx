import { CheckCircle2, XCircle } from "lucide-react";
import { IdText, KeyValue, KindBadge, Money, StatusBadge } from "@/components/common/display";
import { Disclosure } from "@/components/common/Disclosure";
import { ErrorState, LoadingRows } from "@/components/common/states";
import { LifecycleExplorer } from "@/features/observability/LifecycleExplorer";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/dialog";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useTransfer, useTransferLedgerEntries } from "@/hooks/queries";
import { formatDateTime } from "@/lib/format";
import type { LedgerEntry } from "@/types/api";

export function TransferDetailSheet({
  transferId,
  onClose,
  nameOf,
}: {
  transferId: string | null;
  onClose: () => void;
  nameOf: (id: string) => string;
}) {
  return (
    <Sheet open={!!transferId} onOpenChange={(open) => !open && onClose()}>
      <SheetContent>
        <SheetHeader>
          <SheetTitle>Transfer details</SheetTitle>
          <SheetDescription>An immutable record and its double-entry postings, read from the API.</SheetDescription>
        </SheetHeader>
        {transferId && <TransferDetailBody transferId={transferId} nameOf={nameOf} />}
      </SheetContent>
    </Sheet>
  );
}

function TransferDetailBody({ transferId, nameOf }: { transferId: string; nameOf: (id: string) => string }) {
  const transfer = useTransfer(transferId);
  const entries = useTransferLedgerEntries(transferId);

  if (transfer.isLoading) return <LoadingRows rows={6} className="p-5" />;
  if (transfer.error) return <ErrorState error={transfer.error} onRetry={() => void transfer.refetch()} />;
  const t = transfer.data!;

  return (
    <div className="space-y-6 p-5">
      <dl className="grid grid-cols-2 gap-x-4 gap-y-4">
        <KeyValue label="Transaction id" className="col-span-2">
          <IdText id={t.id} />
        </KeyValue>
        <KeyValue label="Amount">
          <Money minor={t.amount_minor} className="text-lg font-semibold" />
        </KeyValue>
        <KeyValue label="Created">{formatDateTime(t.created_at)}</KeyValue>
        <KeyValue label="Type">
          <KindBadge kind={t.kind} />
        </KeyValue>
        <KeyValue label="Status">
          <StatusBadge status={t.status} />
        </KeyValue>
        <KeyValue label="Source">
          <div className="space-y-0.5">
            <div className="truncate">{nameOf(t.source_account_id)}</div>
            <IdText id={t.source_account_id} />
          </div>
        </KeyValue>
        <KeyValue label="Destination">
          <div className="space-y-0.5">
            <div className="truncate">{nameOf(t.destination_account_id)}</div>
            <IdText id={t.destination_account_id} />
          </div>
        </KeyValue>
      </dl>

      <section className="space-y-3" aria-label="Ledger entries">
        <h3 className="text-sm font-semibold">Ledger entries</h3>
        {entries.isLoading ? (
          <LoadingRows rows={2} />
        ) : entries.error ? (
          <ErrorState error={entries.error} onRetry={() => void entries.refetch()} compact />
        ) : (
          <LedgerPair entries={entries.data ?? []} nameOf={nameOf} />
        )}
      </section>

      {t.kind !== "legacy_opening_balance" && (
        <Disclosure variant="boxed" title="Lifecycle: request → commit → events">
          <LifecycleExplorer transferId={t.id} />
        </Disclosure>
      )}
    </div>
  );
}

function LedgerPair({ entries, nameOf }: { entries: LedgerEntry[]; nameOf: (id: string) => string }) {
  // Integer sum of integer cents: exact.
  const net = entries.reduce((sum, e) => sum + e.amount_minor, 0);
  const balanced = entries.length === 2 && net === 0;
  return (
    <div className="space-y-3">
      <div className="rounded-lg border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Role</TableHead>
              <TableHead>Account</TableHead>
              <TableHead className="text-right">Amount</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {entries.map((e) => (
              <TableRow key={e.id}>
                <TableCell className="capitalize">{e.role}</TableCell>
                <TableCell className="max-w-[12rem] truncate">{nameOf(e.account_id)}</TableCell>
                <TableCell className="text-right">
                  <Money minor={e.amount_minor} signed />
                </TableCell>
              </TableRow>
            ))}
            <TableRow className="hover:bg-transparent">
              <TableCell colSpan={2} className="font-medium">
                Net change
              </TableCell>
              <TableCell className="text-right font-medium">
                <Money minor={net} />
              </TableCell>
            </TableRow>
          </TableBody>
        </Table>
      </div>
      <p className={`flex items-center gap-1.5 text-xs ${balanced ? "text-success" : "text-destructive"}`}>
        {balanced ? <CheckCircle2 className="size-3.5" /> : <XCircle className="size-3.5" />}
        {balanced
          ? "Balanced: exactly one debit and one credit that sum to zero."
          : `Unexpected: ${entries.length} entries with net ${net}. Run reconciliation.`}
      </p>
    </div>
  );
}
