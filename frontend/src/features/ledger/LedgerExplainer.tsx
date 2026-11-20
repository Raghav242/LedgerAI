import { BookOpen } from "lucide-react";
import { DataSourceTag, Money } from "@/components/common/display";
import { Disclosure } from "@/components/common/Disclosure";

const EXAMPLE = [
  { account: "Source account", role: "Debit", amount: -2500 },
  { account: "Destination account", role: "Credit", amount: 2500 },
];

const RULES: [string, string][] = [
  ["Append-only.", "Database triggers reject UPDATE and DELETE on ledger entries and transfers; corrections would be new, compensating records."],
  ["Checked at commit.", "A deferred constraint trigger verifies each transfer has exactly one debit and one credit summing to zero."],
  [
    "Money is never created.",
    "Simulated funding is a normal posting from the system funding account, whose balance goes negative, so all balances together always sum to zero.",
  ],
  ["Balances are verifiable.", "Reconciliation recomputes every balance from the ledger and compares it with the stored value."],
];

/** Small, collapsed teaching example. Clearly tagged so it is never mistaken for live data. */
export function LedgerExplainer() {
  const net = EXAMPLE.reduce((s, e) => s + e.amount, 0);
  return (
    <Disclosure variant="boxed" className="bg-card" icon={<BookOpen />} title="How double-entry works" meta={<DataSourceTag kind="example" />}>
      <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
        <div className="rounded-lg border border-dashed p-4" aria-label="Example: a $25.00 transfer">
          <div className="mb-3 text-xs font-medium text-muted-foreground">Example: a $25.00 transfer</div>
          <div className="space-y-2">
            {EXAMPLE.map((e) => (
              <div key={e.account} className="flex items-center justify-between text-sm">
                <span>
                  {e.account} <span className="text-xs text-muted-foreground">({e.role.toLowerCase()})</span>
                </span>
                <Money minor={e.amount} signed className="font-medium" />
              </div>
            ))}
            <div className="flex items-center justify-between border-t pt-2 text-sm font-semibold">
              <span>Net change</span>
              <Money minor={net} />
            </div>
          </div>
        </div>
        <ul className="space-y-2.5 text-sm text-muted-foreground">
          {RULES.map(([title, text]) => (
            <li key={title}>
              <span className="font-medium text-foreground">{title}</span> {text}
            </li>
          ))}
        </ul>
      </div>
    </Disclosure>
  );
}
