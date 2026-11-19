import { ArrowDownLeft, ArrowUpRight, Plus, Users } from "lucide-react";
import { useState } from "react";
import { MAX_PAGE_SIZE } from "@/api/accounts";
import { IdText, KeyValue, Money } from "@/components/common/display";
import { EmptyState, ErrorState, InlineSpinner, LoadingRows } from "@/components/common/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Separator } from "@/components/ui/misc";
import { useAccount, useAccounts } from "@/hooks/queries";
import { formatDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { Account } from "@/types/api";
import { CreateAccountForm } from "./CreateAccountForm";
import { FundAccountForm } from "./FundAccountForm";

export function AccountsPanel({
  selectedId,
  onSelect,
  onUseAs,
}: {
  selectedId: string | null;
  onSelect: (id: string) => void;
  onUseAs?: (role: "source" | "destination", id: string) => void;
}) {
  const query = useAccounts();
  const [createOpen, setCreateOpen] = useState(false);
  const accounts = query.data?.items ?? [];
  // Prefer the list copy (fresh after every invalidation); fall back to a direct read for an
  // account outside the loaded page.
  const inList = accounts.find((a) => a.id === selectedId);
  const direct = useAccount(inList ? null : selectedId);
  const selected = inList ?? direct.data ?? null;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Virtual accounts</CardTitle>
        <CardDescription>
          {query.data ? `${accounts.length}${accounts.length === MAX_PAGE_SIZE ? "+" : ""} merchant accounts` : "Merchant accounts (USD)"}
          {query.isFetching && !query.isLoading && <span className="ml-2"><InlineSpinner label="Refreshing" /></span>}
        </CardDescription>
        <CardAction>
          <Button size="sm" onClick={() => setCreateOpen(true)}>
            <Plus /> New account
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent className="space-y-4">
        {query.isLoading ? (
          <LoadingRows rows={4} />
        ) : query.error ? (
          <ErrorState error={query.error} onRetry={() => void query.refetch()} retrying={query.isFetching} compact />
        ) : accounts.length === 0 ? (
          <EmptyState
            icon={<Users className="size-5" />}
            title="No accounts yet"
            description="Create two accounts, fund one with simulated money, then transfer between them."
            action={
              <Button size="sm" onClick={() => setCreateOpen(true)}>
                <Plus /> Create your first account
              </Button>
            }
          />
        ) : (
          <ul className="max-h-72 divide-y overflow-y-auto rounded-lg border" aria-label="Accounts">
            {accounts.map((account) => (
              <AccountRow key={account.id} account={account} selected={account.id === selectedId} onSelect={onSelect} />
            ))}
          </ul>
        )}
        {accounts.length === MAX_PAGE_SIZE && (
          <p className="text-xs text-muted-foreground">Showing the {MAX_PAGE_SIZE} newest accounts. The API has no search endpoint yet.</p>
        )}

        {selected && (
          <>
            <Separator />
            <section aria-label="Selected account" className="space-y-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="truncate text-sm font-semibold">{selected.name}</div>
                  <IdText id={selected.id} />
                </div>
                <div className="text-right">
                  <div className="text-xs text-muted-foreground">Available balance</div>
                  <Money minor={selected.balance_minor} className="text-xl font-semibold" />
                </div>
              </div>
              <dl className="grid grid-cols-2 gap-3">
                <KeyValue label="Currency">
                  <Badge variant="outline">{selected.currency}</Badge>
                </KeyValue>
                <KeyValue label="Created">{formatDateTime(selected.created_at)}</KeyValue>
              </dl>
              {onUseAs && (
                <div className="flex flex-wrap gap-2">
                  <Button variant="outline" size="sm" onClick={() => onUseAs("source", selected.id)}>
                    <ArrowUpRight /> Send from this account
                  </Button>
                  <Button variant="outline" size="sm" onClick={() => onUseAs("destination", selected.id)}>
                    <ArrowDownLeft /> Send to this account
                  </Button>
                </div>
              )}
              <FundAccountForm account={selected} />
            </section>
          </>
        )}
      </CardContent>

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Create a virtual account</DialogTitle>
            <DialogDescription>New accounts start with a $0.00 balance of simulated money.</DialogDescription>
          </DialogHeader>
          <CreateAccountForm
            onCreated={(account) => {
              setCreateOpen(false);
              onSelect(account.id);
            }}
          />
        </DialogContent>
      </Dialog>
    </Card>
  );
}

function AccountRow({ account, selected, onSelect }: { account: Account; selected: boolean; onSelect: (id: string) => void }) {
  return (
    <li>
      <button
        type="button"
        onClick={() => onSelect(account.id)}
        aria-pressed={selected}
        className={cn(
          "flex w-full items-center justify-between gap-3 px-3 py-2.5 text-left outline-none transition-colors focus-visible:bg-accent",
          selected ? "bg-accent" : "hover:bg-muted/40",
        )}
      >
        <span className="min-w-0">
          <span className="block truncate text-sm font-medium">{account.name}</span>
          <span className="block font-mono text-[11px] text-muted-foreground">{account.id.slice(0, 8)}</span>
        </span>
        <Money minor={account.balance_minor} className="text-sm" />
      </button>
    </li>
  );
}
