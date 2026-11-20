import { PageHeader } from "@/components/common/display";
import { LedgerEntriesCard } from "./LedgerEntriesCard";
import { LedgerExplainer } from "./LedgerExplainer";
import { ReconciliationPanel } from "./ReconciliationPanel";

/** The live verdict first; the postings next; the teaching example last and collapsed. */
export function LedgerPage() {
  return (
    <div className="space-y-6">
      <PageHeader title="Ledger & Reconciliation" description="Do stored balances agree with the immutable double-entry ledger?" />
      <ReconciliationPanel />
      <LedgerExplainer />
      <LedgerEntriesCard />
    </div>
  );
}
