import { DataSourceTag, Money } from "@/components/common/display";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

const EXAMPLE = [
  { account: "Source account", role: "Debit", amount: -2500 },
  { account: "Destination account", role: "Credit", amount: 2500 },
];

/** Static teaching example. Clearly tagged so it is never mistaken for live data. */
export function LedgerExplainer() {
  const net = EXAMPLE.reduce((s, e) => s + e.amount, 0);
  return (
    <Card>
      <CardHeader>
        <CardTitle>How double-entry keeps money correct</CardTitle>
        <CardDescription>Every movement writes two immutable postings that cancel out.</CardDescription>
        <CardAction>
          <DataSourceTag kind="example" />
        </CardAction>
      </CardHeader>
      <CardContent className="grid grid-cols-1 gap-5 md:grid-cols-2">
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
          <li>
            <span className="font-medium text-foreground">Append-only.</span> Database triggers reject UPDATE and DELETE on ledger entries and
            transfers; corrections would be new, compensating records.
          </li>
          <li>
            <span className="font-medium text-foreground">Checked at commit.</span> A deferred constraint trigger verifies each transfer has
            exactly one debit and one credit summing to zero.
          </li>
          <li>
            <span className="font-medium text-foreground">Money is never created.</span> Simulated funding is a normal posting from the system
            funding account, whose balance goes negative, so all balances together always sum to zero.
          </li>
          <li>
            <span className="font-medium text-foreground">Balances are verifiable.</span> Reconciliation recomputes every balance from the
            ledger and compares it with the stored value.
          </li>
        </ul>
      </CardContent>
    </Card>
  );
}
