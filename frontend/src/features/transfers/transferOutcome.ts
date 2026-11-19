/**
 * Classifies the outcome of one POST /api/v1/transfers attempt.
 *
 * The category decides what the user may do next with the Idempotency-Key:
 *
 *  succeeded  money moved (or a replay of a move). Key is consumed.
 *  rejected   a final business answer stored under the key (e.g. INSUFFICIENT_FUNDS).
 *             Retrying the same key replays the same answer; a new attempt needs a new key.
 *  invalid    the request never reached the transfer logic (schema validation) or the key
 *             was already used with a different body. Fix the input; use a new key.
 *  retryable  definitely NOT executed (rate limited, limiter down, lock conflict, same key in
 *             flight). Retry with the SAME key; nothing to reconcile.
 *  ambiguous  MAY have executed (timeout, network loss, 5xx/502/504, OUTCOME_UNKNOWN).
 *             Only a retry with the SAME key can tell, and it can never double-charge.
 *
 * Mirrors the contract in app/api/routes/transfers.py and app/api/error_handlers.py.
 */
import { ApiError, type ApiResponse, type ResponseMeta } from "@/api/client";
import type { Transfer } from "@/types/api";

export type OutcomeCategory = "succeeded" | "rejected" | "invalid" | "retryable" | "ambiguous";

export interface TransferOutcome {
  category: OutcomeCategory;
  /** HTTP status, or null if no response arrived. */
  status: number | null;
  code: string;
  title: string;
  message: string;
  guidance: string;
  transfer: Transfer | null;
  replayed: boolean;
  retryAfterSeconds: number | null;
  details: unknown;
  meta: ResponseMeta | null;
}

/** Codes that guarantee nothing was executed. */
const RETRYABLE_CODES: Record<string, { title: string; guidance: string }> = {
  RATE_LIMITED: {
    title: "Rate limited (429)",
    guidance:
      "The shared write quota (Redis token bucket, global across replicas) is used up. Nothing was executed. Wait for Retry-After, then retry with the same key.",
  },
  RATE_LIMITER_UNAVAILABLE: {
    title: "Rate limiter unavailable (503)",
    guidance:
      "Redis is unreachable, so writes fail closed and nothing was executed. Retry with the same key once Redis is back.",
  },
  CONCURRENCY_CONFLICT: {
    title: "Concurrency conflict (503)",
    guidance:
      "The transaction waited too long for a row lock and was rolled back. Nothing was committed. Retry with the same key.",
  },
  IDEMPOTENCY_REQUEST_IN_PROGRESS: {
    title: "Same key still in progress (409)",
    guidance:
      "Another request with this key is still running. Retry with the same key; you will receive that request's outcome.",
  },
};

const REJECTION_TITLES: Record<string, string> = {
  INSUFFICIENT_FUNDS: "Insufficient funds",
  ACCOUNT_NOT_FOUND: "Account not found",
  SAME_ACCOUNT_TRANSFER: "Same source and destination",
  CURRENCY_MISMATCH: "Currency mismatch",
  SYSTEM_ACCOUNT_NOT_ALLOWED: "System account not allowed",
  INVALID_AMOUNT: "Invalid amount",
};

const REJECTION_GUIDANCE =
  "This is the key's final, stored outcome: retrying with the same key replays the same rejection and moves no money. Fix the cause and start a new attempt (new key).";

const AMBIGUOUS_GUIDANCE =
  "The transfer may or may not have been committed. Retry with the SAME idempotency key: if it committed you get the original result (no second charge); if not, it executes now. Do not start a new transfer.";

function base(meta: ResponseMeta | null): Pick<TransferOutcome, "meta" | "replayed" | "retryAfterSeconds"> {
  return {
    meta,
    replayed: meta?.idempotentReplayed ?? false,
    retryAfterSeconds: meta?.retryAfterSeconds ?? null,
  };
}

export function outcomeFromSuccess(response: ApiResponse<Transfer>): TransferOutcome {
  const replayed = response.meta.idempotentReplayed;
  return {
    ...base(response.meta),
    category: "succeeded",
    status: response.meta.status,
    code: "COMPLETED",
    title: replayed ? "Replayed original success" : "Transfer completed",
    message: replayed
      ? "The server recognised this idempotency key and returned the original response. No money moved this time."
      : "Both balances, the transfer record, its two ledger entries and the outbox event committed in one transaction.",
    guidance: replayed
      ? "Idempotent-Replayed: true. Sending the same key again is always safe."
      : "This key is now bound to this transfer. Start a new transfer to generate a new key.",
    transfer: response.data,
    details: null,
  };
}

export function outcomeFromError(error: unknown): TransferOutcome {
  if (!(error instanceof ApiError)) {
    return {
      ...base(null),
      category: "ambiguous",
      status: null,
      code: "UNKNOWN_ERROR",
      title: "Unexpected client error",
      message: error instanceof Error ? error.message : String(error),
      guidance: AMBIGUOUS_GUIDANCE,
      transfer: null,
      details: null,
    };
  }

  const common = { ...base(error.meta), status: error.status, code: error.code, message: error.message, details: error.details, transfer: null };

  if (error.kind === "timeout" || error.kind === "network" || error.kind === "aborted") {
    return {
      ...common,
      category: "ambiguous",
      title: error.kind === "timeout" ? "Request timed out" : "No response from the API",
      guidance: AMBIGUOUS_GUIDANCE,
    };
  }

  const retryable = RETRYABLE_CODES[error.code];
  if (retryable) return { ...common, category: "retryable", ...retryable };

  if (error.code === "IDEMPOTENCY_KEY_REUSED") {
    return {
      ...common,
      category: "invalid",
      title: "Idempotency key reused (422)",
      guidance:
        "This key was already used with a different source, destination or amount. A key identifies exactly one request. Start a new attempt with a new key.",
    };
  }
  if (error.code === "VALIDATION_ERROR") {
    return {
      ...common,
      category: "invalid",
      title: "Request validation failed (422)",
      guidance: "The request was rejected before any transfer logic ran; the key was not used. Fix the input and submit again.",
    };
  }

  const rejectionTitle = REJECTION_TITLES[error.code];
  if (rejectionTitle) {
    return { ...common, category: "rejected", title: `${rejectionTitle} (${error.status})`, guidance: REJECTION_GUIDANCE };
  }

  // OUTCOME_UNKNOWN, INTERNAL_ERROR, gateway 502/504 and anything unrecognised.
  return {
    ...common,
    category: "ambiguous",
    title: error.code === "OUTCOME_UNKNOWN" ? "Outcome unknown (500)" : `Uncertain outcome (${error.status ?? "no status"})`,
    guidance: AMBIGUOUS_GUIDANCE,
  };
}

/** After these outcomes the key must be kept and the request body frozen. */
export function mustReuseKey(category: OutcomeCategory): boolean {
  return category === "retryable" || category === "ambiguous";
}
