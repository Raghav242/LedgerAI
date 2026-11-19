import { zodResolver } from "@hookform/resolvers/zod";
import { KeyRound, Lock, Loader2, RefreshCw, Send } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useForm, useWatch } from "react-hook-form";
import { CopyButton } from "@/components/common/display";
import { FormField } from "@/components/common/FormField";
import { describedBy } from "@/lib/a11y";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/misc";
import { NativeSelect } from "@/components/ui/native-select";
import { IDEMPOTENCY_KEY_PATTERN, newIdempotencyKey } from "@/lib/ids";
import { formatMinor, parseUsdToMinor, SUPPORTED_CURRENCIES } from "@/lib/money";
import type { Account } from "@/types/api";
import type { TransferAttemptState } from "./transferAttempt";
import { buildTransferSchema, emptyTransferForm, toTransferRequest, type TransferFormValues } from "./transferSchema";
import type { useTransferAttempt } from "./useTransferAttempt";

export interface AccountPreset {
  role: "source" | "destination";
  id: string;
  /** Changes on every request so the same preset can be applied twice. */
  nonce: number;
}

type Attempt = ReturnType<typeof useTransferAttempt>;

export function TransferForm({ accounts, attempt, preset }: { accounts: Account[]; attempt: Attempt; preset?: AccountPreset | null }) {
  const { state } = attempt;
  const pinned = state.keyStatus === "pinned";
  const locked = pinned || state.inFlight;
  const knownIds = useMemo(() => new Set(accounts.map((a) => a.id)), [accounts]);
  const schema = useMemo(() => buildTransferSchema(knownIds), [knownIds]);

  const form = useForm<TransferFormValues>({ resolver: zodResolver(schema), defaultValues: emptyTransferForm, mode: "onSubmit" });
  const { errors } = form.formState;

  useEffect(() => {
    if (preset && !locked) form.setValue(preset.role, preset.id, { shouldValidate: form.formState.isSubmitted });
  }, [preset, locked, form]);

  const source = useWatch({ control: form.control, name: "source" });
  const amountText = useWatch({ control: form.control, name: "amount" });
  const sourceAccount = accounts.find((a) => a.id === source);
  const parsedAmount = parseUsdToMinor(amountText);
  const exceedsBalance = sourceAccount && parsedAmount.ok && parsedAmount.minor > sourceAccount.balance_minor;

  const onSubmit = form.handleSubmit((values) => {
    void attempt.submit(toTransferRequest(values));
  });

  return (
    <form onSubmit={onSubmit} noValidate className="grid gap-4" aria-label="Transfer funds">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <FormField id="transfer-source" label="Source account" error={errors.source?.message}>
          <NativeSelect
            id="transfer-source"
            disabled={locked}
            aria-invalid={!!errors.source}
            aria-describedby={describedBy("transfer-source", errors.source?.message)}
            {...form.register("source")}
          >
            <option value="">Select account…</option>
            {accounts.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name} · {formatMinor(a.balance_minor)}
              </option>
            ))}
          </NativeSelect>
        </FormField>
        <FormField id="transfer-destination" label="Destination account" error={errors.destination?.message}>
          <NativeSelect
            id="transfer-destination"
            disabled={locked}
            aria-invalid={!!errors.destination}
            aria-describedby={describedBy("transfer-destination", errors.destination?.message)}
            {...form.register("destination")}
          >
            <option value="">Select account…</option>
            {accounts.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </NativeSelect>
        </FormField>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-[1fr_8rem]">
        <FormField
          id="transfer-amount"
          label="Amount (USD)"
          error={errors.amount?.message}
          hint={
            parsedAmount.ok ? (
              <>
                Sent as <code className="font-mono">amount_minor: {parsedAmount.minor}</code>
                {exceedsBalance && <span className="text-warning"> · exceeds the displayed balance; the server will decide</span>}
              </>
            ) : (
              "Up to two decimal places. Converted to integer cents without floating point."
            )
          }
        >
          <Input
            id="transfer-amount"
            inputMode="decimal"
            placeholder="25.00"
            autoComplete="off"
            disabled={locked}
            className="font-mono"
            aria-invalid={!!errors.amount}
            aria-describedby={describedBy("transfer-amount", errors.amount?.message, true)}
            {...form.register("amount")}
          />
        </FormField>
        <FormField id="transfer-currency" label="Currency" error={errors.currency?.message}>
          <NativeSelect id="transfer-currency" disabled={locked} {...form.register("currency")}>
            {SUPPORTED_CURRENCIES.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </NativeSelect>
        </FormField>
      </div>

      <IdempotencyKeyInspector state={state} onSetKey={attempt.setKey} />

      {pinned && (
        <Alert variant="warning">
          <Lock />
          <AlertTitle>Request locked to its idempotency key</AlertTitle>
          <AlertDescription>
            The last attempt did not reach a final outcome. Retry with the same key from the result panel, or explicitly start a new
            transfer. Editing the request now would make it a different request under the same key.
          </AlertDescription>
        </Alert>
      )}

      <div className="flex flex-wrap items-center justify-end gap-2">
        {state.keyStatus === "consumed" && (
          <span className="mr-auto text-xs text-muted-foreground">Submitting starts a new logical transfer with a new key.</span>
        )}
        <Button type="submit" disabled={locked}>
          {state.inFlight ? <Loader2 className="animate-spin" /> : <Send />}
          {state.inFlight ? "Sending…" : "Send transfer"}
        </Button>
      </div>
    </form>
  );
}

const KEY_STATUS: Record<TransferAttemptState["keyStatus"], { label: string; variant: "info" | "warning" | "muted"; hint: string }> = {
  fresh: { label: "Unused", variant: "info", hint: "Generated for this logical transfer. It is sent as the Idempotency-Key header." },
  pinned: {
    label: "Pinned for retry",
    variant: "warning",
    hint: "The outcome is not final. Every retry must reuse exactly this key and request body.",
  },
  consumed: {
    label: "Final outcome stored",
    variant: "muted",
    hint: "The server stored this key's final outcome. A new transfer will get a new key.",
  },
};

function IdempotencyKeyInspector({ state, onSetKey }: { state: TransferAttemptState; onSetKey: (key: string) => void }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [draftError, setDraftError] = useState<string>();
  const status = KEY_STATUS[state.keyStatus];
  const canEdit = state.keyStatus === "fresh" && !state.inFlight;

  function applyDraft() {
    if (!IDEMPOTENCY_KEY_PATTERN.test(draft)) {
      setDraftError("1-255 visible ASCII characters, no spaces");
      return;
    }
    onSetKey(draft);
    setEditing(false);
  }

  return (
    <div className="rounded-lg border bg-muted/30 p-3" aria-label="Idempotency key">
      <div className="flex flex-wrap items-center gap-2">
        <KeyRound className="size-4 text-muted-foreground" aria-hidden />
        <span className="text-sm font-medium">Idempotency-Key</span>
        <Badge variant={status.variant}>{status.label}</Badge>
        <div className="ml-auto flex items-center gap-1">
          {canEdit && !editing && (
            <>
              <Button
                variant="ghost"
                size="sm"
                className="h-7 text-xs"
                onClick={() => {
                  setDraft(state.key);
                  setDraftError(undefined);
                  setEditing(true);
                }}
              >
                Edit
              </Button>
              <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={() => onSetKey(newIdempotencyKey())}>
                <RefreshCw className="size-3" /> Regenerate
              </Button>
            </>
          )}
        </div>
      </div>
      {editing ? (
        <div className="mt-2 grid gap-1.5">
          <div className="flex gap-2">
            <Input
              aria-label="Custom idempotency key"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              className="h-8 font-mono text-xs"
              aria-invalid={!!draftError}
            />
            <Button size="sm" variant="secondary" onClick={applyDraft}>
              Use key
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
              Cancel
            </Button>
          </div>
          {draftError && <p className="text-xs text-destructive">{draftError}</p>}
          <p className="text-xs text-muted-foreground">
            Advanced: reuse a key from an earlier transfer with a different body to observe 422 IDEMPOTENCY_KEY_REUSED.
          </p>
        </div>
      ) : (
        <div className="mt-1.5 flex items-center gap-1">
          <code className="truncate font-mono text-xs" data-testid="idempotency-key">
            {state.key}
          </code>
          <CopyButton value={state.key} label="Copy idempotency key" />
        </div>
      )}
      <p className="mt-1 text-xs text-muted-foreground">{status.hint}</p>
    </div>
  );
}
