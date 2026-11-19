import { Money } from "@/components/common/display";
import type { Invariant } from "@/components/lab/InvariantCheckList";
import { formatCount } from "@/lib/format";
import type { Reconciliation } from "@/types/api";

/** Maps the backend's ReconciliationRead fields to named invariants. No values are invented. */
export function reconciliationInvariants(r: Reconciliation): Invariant[] {
  return [
    {
      id: "balances-match-ledger",
      label: "Every stored balance equals the sum of its ledger entries",
      description: "accounts.balance_minor is a cache; the ledger is the source of truth.",
      status: r.balance_mismatches.length === 0 ? "pass" : "fail",
      observed: `${r.balance_mismatches.length} mismatched account(s) across ${formatCount(r.accounts_checked)} checked`,
    },
    {
      id: "ledger-sums-to-zero",
      label: "All ledger entries sum to zero",
      description: "Each posting has an equal and opposite partner.",
      status: r.ledger_total_minor === 0 ? "pass" : "fail",
      observed: (
        <>
          Σ ledger = <Money minor={r.ledger_total_minor} /> over {formatCount(r.ledger_entries_checked)} entries
        </>
      ),
    },
    {
      id: "balances-sum-to-zero",
      label: "All account balances sum to zero",
      description: "The system funding account goes negative by exactly the simulated money issued to merchants.",
      status: r.balance_total_minor === 0 ? "pass" : "fail",
      observed: (
        <>
          Σ balances = <Money minor={r.balance_total_minor} />
        </>
      ),
    },
    {
      id: "transfers-balanced",
      label: "Every transfer has exactly two entries that net to zero",
      status: r.unbalanced_transfer_ids.length === 0 ? "pass" : "fail",
      observed: `${r.unbalanced_transfer_ids.length} unbalanced transfer(s)`,
    },
  ];
}
