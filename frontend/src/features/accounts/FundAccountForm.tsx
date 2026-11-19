import { Loader2, PiggyBank } from "lucide-react";
import { useState, type FormEvent } from "react";
import { isApiError } from "@/api/client";
import { FormField } from "@/components/common/FormField";
import { describedBy } from "@/lib/a11y";
import { describeError } from "@/lib/errors";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/misc";
import { useFundAccount } from "@/hooks/queries";
import { formatMinor, parseUsdToMinor } from "@/lib/money";
import type { Account } from "@/types/api";

const PRESETS = [10_000, 100_000, 1_000_000]; // $100, $1,000, $10,000 in cents

/**
 * Calls the DEVELOPMENT-ONLY funding endpoint, which moves simulated money from the system
 * funding account. Not idempotent by design (each click is a new funding).
 */
export function FundAccountForm({ account }: { account: Account }) {
  const mutation = useFundAccount();
  const [amount, setAmount] = useState("");
  const [fieldError, setFieldError] = useState<string>();
  const [lastFunded, setLastFunded] = useState<number | null>(null);

  async function fund(minor: number) {
    setLastFunded(null);
    try {
      await mutation.mutateAsync({ accountId: account.id, amountMinor: minor });
      setLastFunded(minor);
      setAmount("");
    } catch {
      // rendered below
    }
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    const parsed = parseUsdToMinor(amount);
    if (!parsed.ok) {
      setFieldError(parsed.error);
      return;
    }
    setFieldError(undefined);
    void fund(parsed.minor);
  }

  const notEnabled = isApiError(mutation.error) && (mutation.error.status === 404 || mutation.error.status === 405) && mutation.error.code !== "ACCOUNT_NOT_FOUND";

  return (
    <form onSubmit={onSubmit} noValidate className="grid gap-3" aria-label="Add simulated funds">
      <FormField
        id="fund-amount"
        label="Add simulated funds (USD)"
        error={fieldError}
        hint="Development-only endpoint. Mints virtual money from the system funding account."
      >
        <div className="flex gap-2">
          <Input
            id="fund-amount"
            inputMode="decimal"
            placeholder="100.00"
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
            aria-invalid={!!fieldError}
            aria-describedby={describedBy("fund-amount", fieldError, true)}
            className="font-mono"
          />
          <Button type="submit" variant="secondary" disabled={mutation.isPending}>
            {mutation.isPending ? <Loader2 className="animate-spin" /> : <PiggyBank />}
            Fund
          </Button>
        </div>
      </FormField>
      <div className="flex flex-wrap gap-2">
        {PRESETS.map((minor) => (
          <Button key={minor} variant="outline" size="sm" disabled={mutation.isPending} onClick={() => void fund(minor)}>
            +{formatMinor(minor)}
          </Button>
        ))}
      </div>
      {lastFunded !== null && !mutation.isPending && (
        <p role="status" className="text-xs text-success">
          Added {formatMinor(lastFunded)} of simulated funds.
        </p>
      )}
      {mutation.error && (
        <Alert variant="destructive">
          <AlertTitle>{notEnabled ? "Funding endpoint not enabled" : describeError(mutation.error).title}</AlertTitle>
          <AlertDescription>
            {notEnabled
              ? "Start the backend with ENABLE_DEV_ENDPOINTS=true (development only) to add simulated funds."
              : describeError(mutation.error).message}
          </AlertDescription>
        </Alert>
      )}
    </form>
  );
}
