import { PageHeader } from "@/components/common/display";
import { LedgerEntriesCard } from "./LedgerEntriesCard";
import { LedgerExplainer } from "./LedgerExplainer";
import { ReconciliationPanel } from "./ReconciliationPanel";

export function LedgerPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="Ledger & Reconciliation"
        description="Balances are a cache; the immutable double-entry ledger is the source of truth. Reconciliation proves the two agree."
      />
      <LedgerExplainer />
      <ReconciliationPanel />
      <LedgerEntriesCard />
    </div>
  );
}
