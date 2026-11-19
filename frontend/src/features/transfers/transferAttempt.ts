/**
 * Idempotency-key lifecycle for one *logical* transfer, as a pure reducer.
 *
 *   fresh ──submit──► (in flight) ──► succeeded / rejected / key reused ──► consumed
 *     ▲                     │
 *     │                     └──► retryable / ambiguous ──► pinned ──retry (same key)──┐
 *     │                                                     ▲                         │
 *     └──────────── startNew (explicit user action) ────────┴─────────────────────────┘
 *
 * Invariants:
 *  - A key is generated only by `init` or `startNew`, i.e. when the user begins a NEW
 *    logical transfer. No outcome ever generates a key automatically.
 *  - While `pinned`, the request body is frozen: the only way to send it is `retry`, with
 *    the same key and the same body (a different body would be IDEMPOTENCY_KEY_REUSED).
 *  - Submitting while `consumed` starts a new logical transfer (new key) because the
 *    previous key already has a final outcome.
 */
import { newIdempotencyKey } from "@/lib/ids";
import type { TransferCreate } from "@/types/api";
import type { TransferOutcome } from "./transferOutcome";
import { mustReuseKey } from "./transferOutcome";

export type KeyStatus = "fresh" | "pinned" | "consumed";

export interface AttemptRecord {
  attempt: number;
  key: string;
  request: TransferCreate;
  startedAt: number;
  finishedAt: number | null;
  outcome: TransferOutcome | null;
  mode: "submit" | "retry" | "replay";
}

export interface TransferAttemptState {
  key: string;
  keyStatus: KeyStatus;
  /** The body bound to the key while pinned (or after it was consumed). */
  boundRequest: TransferCreate | null;
  inFlight: boolean;
  attempts: AttemptRecord[];
  lastOutcome: TransferOutcome | null;
}

export type TransferAttemptAction =
  | { type: "sendStarted"; request: TransferCreate; key: string; mode: AttemptRecord["mode"]; at: number }
  | { type: "sendFinished"; outcome: TransferOutcome; at: number }
  | { type: "startNew"; key: string }
  | { type: "setKey"; key: string };

export function initialAttemptState(key: string = newIdempotencyKey()): TransferAttemptState {
  return { key, keyStatus: "fresh", boundRequest: null, inFlight: false, attempts: [], lastOutcome: null };
}

function keyStatusAfter(outcome: TransferOutcome): KeyStatus {
  if (mustReuseKey(outcome.category)) return "pinned";
  // Schema validation runs before the key is claimed, so the key is still unused.
  if (outcome.category === "invalid" && outcome.code === "VALIDATION_ERROR") return "fresh";
  return "consumed";
}

export function transferAttemptReducer(state: TransferAttemptState, action: TransferAttemptAction): TransferAttemptState {
  switch (action.type) {
    case "sendStarted": {
      if (state.inFlight) return state;
      // A new logical transfer after a final outcome begins with the key passed in.
      const continuing = action.mode !== "submit" || state.keyStatus === "fresh";
      const attempts = continuing ? state.attempts : [];
      return {
        ...state,
        key: action.key,
        keyStatus: continuing ? state.keyStatus : "fresh",
        boundRequest: action.request,
        inFlight: true,
        lastOutcome: continuing ? state.lastOutcome : null,
        attempts: [
          ...attempts,
          {
            attempt: attempts.length + 1,
            key: action.key,
            request: action.request,
            startedAt: action.at,
            finishedAt: null,
            outcome: null,
            mode: action.mode,
          },
        ],
      };
    }
    case "sendFinished": {
      const attempts = state.attempts.slice();
      const last = attempts.at(-1);
      if (last) attempts[attempts.length - 1] = { ...last, finishedAt: action.at, outcome: action.outcome };
      const keyStatus = keyStatusAfter(action.outcome);
      return {
        ...state,
        inFlight: false,
        attempts,
        lastOutcome: action.outcome,
        keyStatus,
        boundRequest: keyStatus === "fresh" ? null : state.boundRequest,
      };
    }
    case "startNew":
      if (state.inFlight) return state;
      return initialAttemptState(action.key);
    case "setKey":
      // Manual keys (advanced demo) only before the key is bound to a request.
      if (state.inFlight || state.keyStatus !== "fresh") return state;
      return { ...state, key: action.key };
  }
}

/**
 * Which key the next *submit* (not retry) must use. Fresh -> the current key; consumed ->
 * a new one, because the user is starting a new logical transfer. Never called when pinned.
 */
export function keyForSubmit(state: TransferAttemptState, generate: () => string = newIdempotencyKey): string {
  if (state.keyStatus === "pinned") throw new Error("A pinned key must be retried, not resubmitted");
  return state.keyStatus === "consumed" ? generate() : state.key;
}
