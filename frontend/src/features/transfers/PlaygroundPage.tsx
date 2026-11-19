import { useState } from "react";
import { PageHeader } from "@/components/common/display";
import { ErrorState, LoadingRows } from "@/components/common/states";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useAccountDirectory } from "@/hooks/useAccountDirectory";
import { AccountsPanel } from "@/features/accounts/AccountsPanel";
import { useAccounts } from "@/hooks/queries";
import { TransferForm, type AccountPreset } from "./TransferForm";
import { TransferResultPanel } from "./TransferResultPanel";
import { useTransferAttempt } from "./useTransferAttempt";

const PIPELINE = [
  ["NGINX", "Round-robins the request to an API replica and stamps an X-Request-ID."],
  ["Rate limit", "Atomic Redis token bucket shared by every replica. Over quota → 429, nothing runs."],
  ["Claim key", "INSERT the Idempotency-Key claim. A duplicate key waits, then replays the stored outcome."],
  ["Lock rows", "SELECT … FOR UPDATE on both accounts in ascending id order, so transfers cannot deadlock."],
  ["Validate", "Funds, currency and account checks run against the locked, current balances."],
  ["Post", "Update both balances, insert the transfer, one debit and one credit ledger entry, and an outbox event."],
  ["Commit", "One COMMIT makes money movement, ledger, event and idempotency outcome durable together."],
] as const;

export function PlaygroundPage() {
  const accountsQuery = useAccounts();
  const directory = useAccountDirectory();
  const attempt = useTransferAttempt();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [preset, setPreset] = useState<AccountPreset | null>(null);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Transaction Playground"
        description="Create virtual accounts, add simulated funds and send idempotent transfers through the real API: NGINX, multiple replicas, Redis rate limiting and PostgreSQL row locks."
      />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
        <div className="space-y-6 lg:col-span-5">
          <AccountsPanel
            selectedId={selectedId}
            onSelect={setSelectedId}
            onUseAs={(role, id) => setPreset({ role, id, nonce: Date.now() })}
          />
          <Card>
            <CardHeader>
              <CardTitle>What happens inside a transfer</CardTitle>
              <CardDescription>The server-side path of POST /api/v1/transfers (design summary, not live tracing).</CardDescription>
            </CardHeader>
            <CardContent>
              <ol className="space-y-2.5">
                {PIPELINE.map(([step, text], i) => (
                  <li key={step} className="flex gap-3 text-sm">
                    <span className="mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-full border font-mono text-[10px] text-muted-foreground">
                      {i + 1}
                    </span>
                    <span>
                      <span className="font-medium">{step}.</span> <span className="text-muted-foreground">{text}</span>
                    </span>
                  </li>
                ))}
              </ol>
            </CardContent>
          </Card>
        </div>

        <div className="space-y-6 lg:col-span-7">
          <Card>
            <CardHeader>
              <CardTitle>New transfer</CardTitle>
              <CardDescription>Simulated USD. The amount is sent as integer cents with a client-generated Idempotency-Key.</CardDescription>
            </CardHeader>
            <CardContent>
              {accountsQuery.isLoading ? (
                <LoadingRows rows={3} />
              ) : accountsQuery.error ? (
                <ErrorState error={accountsQuery.error} onRetry={() => void accountsQuery.refetch()} compact />
              ) : (
                <TransferForm accounts={directory.accounts} attempt={attempt} preset={preset} />
              )}
            </CardContent>
          </Card>
          <TransferResultPanel
            state={attempt.state}
            nameOf={directory.nameOf}
            onRetry={() => void attempt.retry()}
            onReplay={() => void attempt.replay()}
            onStartNew={attempt.startNew}
          />
        </div>
      </div>
    </div>
  );
}
