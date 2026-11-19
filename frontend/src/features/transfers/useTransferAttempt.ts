import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useReducer, useRef } from "react";
import { transfersApi } from "@/api/transfers";
import { invalidateAfterMoneyMovement } from "@/hooks/queries";
import { newIdempotencyKey } from "@/lib/ids";
import type { TransferCreate } from "@/types/api";
import { initialAttemptState, keyForSubmit, transferAttemptReducer, type AttemptRecord } from "./transferAttempt";
import { outcomeFromError, outcomeFromSuccess, type TransferOutcome } from "./transferOutcome";

export type SendTransfer = typeof transfersApi.create;

/**
 * Drives one logical transfer through the reducer in transferAttempt.ts. Nothing here
 * retries on its own: every network call is a deliberate user action.
 */
export function useTransferAttempt(send: SendTransfer = transfersApi.create) {
  const queryClient = useQueryClient();
  const [state, dispatch] = useReducer(transferAttemptReducer, undefined, () => initialAttemptState());
  // Synchronous guard against double clicks before React re-renders with inFlight=true.
  const inFlightRef = useRef(false);

  const execute = useCallback(
    async (request: TransferCreate, key: string, mode: AttemptRecord["mode"]): Promise<TransferOutcome | null> => {
      if (inFlightRef.current) return null;
      inFlightRef.current = true;
      dispatch({ type: "sendStarted", request, key, mode, at: Date.now() });
      let outcome: TransferOutcome;
      try {
        outcome = outcomeFromSuccess(await send(request, key));
      } catch (error) {
        outcome = outcomeFromError(error);
      } finally {
        inFlightRef.current = false;
      }
      dispatch({ type: "sendFinished", outcome, at: Date.now() });
      // Refresh balances/history only after a confirmed success.
      if (outcome.category === "succeeded") void invalidateAfterMoneyMovement(queryClient);
      return outcome;
    },
    [queryClient, send],
  );

  /** Send a (possibly new) logical transfer. */
  const submit = useCallback(
    (request: TransferCreate) => execute(request, keyForSubmit(state), "submit"),
    [execute, state],
  );

  /** Resend the bound request with the SAME key (after a retryable/ambiguous outcome). */
  const retry = useCallback(() => {
    if (state.keyStatus !== "pinned" || !state.boundRequest) return Promise.resolve(null);
    return execute(state.boundRequest, state.key, "retry");
  }, [execute, state]);

  /** Demonstrate idempotency: resend a finished request with its SAME key. */
  const replay = useCallback(() => {
    if (state.keyStatus !== "consumed" || !state.boundRequest) return Promise.resolve(null);
    return execute(state.boundRequest, state.key, "replay");
  }, [execute, state]);

  const startNew = useCallback(() => dispatch({ type: "startNew", key: newIdempotencyKey() }), []);
  const setKey = useCallback((key: string) => dispatch({ type: "setKey", key }), []);

  return { state, submit, retry, replay, startNew, setKey };
}
