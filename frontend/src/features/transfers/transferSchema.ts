import { z } from "zod";
import { isSupportedCurrency, parseUsdToMinor, SUPPORTED_CURRENCIES } from "@/lib/money";
import type { TransferCreate } from "@/types/api";

export interface TransferFormValues {
  source: string;
  destination: string;
  amount: string;
  currency: string;
}

export const emptyTransferForm: TransferFormValues = { source: "", destination: "", amount: "", currency: SUPPORTED_CURRENCIES[0] };

/**
 * Client-side checks that mirror the backend's own validation, for fast feedback only.
 * The server stays authoritative: balance sufficiency is deliberately NOT checked here, so
 * the playground can demonstrate the server's INSUFFICIENT_FUNDS rejection.
 */
export function buildTransferSchema(knownAccountIds: ReadonlySet<string>) {
  return z
    .object({
      source: z.string().min(1, "Select a source account"),
      destination: z.string().min(1, "Select a destination account"),
      amount: z.string(),
      currency: z.string(),
    })
    .superRefine((v, ctx) => {
      if (v.source && !knownAccountIds.has(v.source)) {
        ctx.addIssue({ code: "custom", path: ["source"], message: "Select an existing account" });
      }
      if (v.destination && !knownAccountIds.has(v.destination)) {
        ctx.addIssue({ code: "custom", path: ["destination"], message: "Select an existing account" });
      }
      if (v.source && v.destination && v.source === v.destination) {
        ctx.addIssue({ code: "custom", path: ["destination"], message: "Source and destination must be different accounts" });
      }
      const amount = parseUsdToMinor(v.amount);
      if (!amount.ok) ctx.addIssue({ code: "custom", path: ["amount"], message: amount.error });
      if (!isSupportedCurrency(v.currency)) {
        ctx.addIssue({ code: "custom", path: ["currency"], message: `Unsupported currency: ${v.currency}` });
      }
    });
}

/** Only call with values that passed the schema. */
export function toTransferRequest(values: TransferFormValues): TransferCreate {
  const amount = parseUsdToMinor(values.amount);
  if (!amount.ok) throw new Error(amount.error);
  if (!isSupportedCurrency(values.currency)) throw new Error(`Unsupported currency: ${values.currency}`);
  return {
    source_account_id: values.source,
    destination_account_id: values.destination,
    amount_minor: amount.minor,
    currency: values.currency,
  };
}
