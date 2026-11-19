import { describe, expect, it } from "vitest";
import { ApiError } from "@/api/client";
import type { TransferCreate } from "@/types/api";
import { initialAttemptState, keyForSubmit, transferAttemptReducer, type TransferAttemptState } from "./transferAttempt";
import { outcomeFromError, outcomeFromSuccess } from "./transferOutcome";

const request: TransferCreate = { source_account_id: "a", destination_account_id: "b", amount_minor: 100, currency: "USD" };
const ambiguous = outcomeFromError(new ApiError({ kind: "timeout", status: null, code: "TIMEOUT", message: "t" }));
const rateLimited = outcomeFromError(new ApiError({ kind: "http", status: 429, code: "RATE_LIMITED", message: "r" }));
const rejected = outcomeFromError(new ApiError({ kind: "http", status: 409, code: "INSUFFICIENT_FUNDS", message: "f" }));
const validation = outcomeFromError(new ApiError({ kind: "http", status: 422, code: "VALIDATION_ERROR", message: "v" }));
const success = outcomeFromSuccess({
  data: { ...request, id: "t", status: "completed", kind: "transfer", created_at: "2026-11-18T00:00:00Z" },
  meta: { status: 201, requestId: null, servedBy: null, rateLimit: { policy: null, limit: null, remaining: null }, retryAfterSeconds: null, idempotentReplayed: false, durationMs: 1 },
});

function send(state: TransferAttemptState, mode: "submit" | "retry" | "replay", outcome: typeof success, key = state.key) {
  const started = transferAttemptReducer(state, { type: "sendStarted", request, key, mode, at: 1 });
  return transferAttemptReducer(started, { type: "sendFinished", outcome, at: 2 });
}

describe("idempotency key lifecycle", () => {
  it("starts fresh with a generated key", () => {
    const s = initialAttemptState();
    expect(s.keyStatus).toBe("fresh");
    expect(s.key).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("pins the key after an ambiguous outcome and reuses it on retry", () => {
    let s = initialAttemptState("key-1");
    s = send(s, "submit", ambiguous);
    expect(s.keyStatus).toBe("pinned");
    expect(s.boundRequest).toEqual(request);
    expect(() => keyForSubmit(s)).toThrow(/pinned/);

    s = send(s, "retry", success, s.key);
    expect(s.key).toBe("key-1");
    expect(s.attempts.map((a) => a.key)).toEqual(["key-1", "key-1"]);
    expect(s.attempts.map((a) => a.mode)).toEqual(["submit", "retry"]);
    expect(s.keyStatus).toBe("consumed");
  });

  it("pins the key after 429 (nothing executed, still same key)", () => {
    const s = send(initialAttemptState("key-1"), "submit", rateLimited);
    expect(s.keyStatus).toBe("pinned");
    expect(s.key).toBe("key-1");
  });

  it("never generates a new key on its own after an uncertain outcome", () => {
    let s = initialAttemptState("key-1");
    for (let i = 0; i < 3; i++) s = send(s, i === 0 ? "submit" : "retry", ambiguous, s.key);
    expect(new Set(s.attempts.map((a) => a.key))).toEqual(new Set(["key-1"]));
  });

  it("a final rejection consumes the key; the next submit uses a new one", () => {
    let s = send(initialAttemptState("key-1"), "submit", rejected);
    expect(s.keyStatus).toBe("consumed");
    const next = keyForSubmit(s, () => "key-2");
    expect(next).toBe("key-2");
    s = send(s, "submit", success, next);
    expect(s.key).toBe("key-2");
    expect(s.attempts).toHaveLength(1); // a new logical transfer starts a new log
  });

  it("schema validation errors leave the key unused", () => {
    const s = send(initialAttemptState("key-1"), "submit", validation);
    expect(s.keyStatus).toBe("fresh");
    expect(keyForSubmit(s, () => "other")).toBe("key-1");
  });

  it("replay after success keeps the key", () => {
    let s = send(initialAttemptState("key-1"), "submit", success);
    s = send(s, "replay", success, s.key);
    expect(s.attempts.map((a) => a.key)).toEqual(["key-1", "key-1"]);
    expect(s.keyStatus).toBe("consumed");
  });

  it("manual key edits are refused once the key is bound", () => {
    const pinned = send(initialAttemptState("key-1"), "submit", ambiguous);
    expect(transferAttemptReducer(pinned, { type: "setKey", key: "evil" }).key).toBe("key-1");
    const fresh = transferAttemptReducer(initialAttemptState("key-1"), { type: "setKey", key: "custom" });
    expect(fresh.key).toBe("custom");
  });

  it("ignores a second send while one is in flight", () => {
    const s = transferAttemptReducer(initialAttemptState("k"), { type: "sendStarted", request, key: "k", mode: "submit", at: 1 });
    expect(transferAttemptReducer(s, { type: "sendStarted", request, key: "k", mode: "submit", at: 2 })).toBe(s);
    expect(transferAttemptReducer(s, { type: "startNew", key: "k2" })).toBe(s);
  });

  it("startNew is the only way to abandon a pinned key", () => {
    const pinned = send(initialAttemptState("key-1"), "submit", ambiguous);
    const s = transferAttemptReducer(pinned, { type: "startNew", key: "key-2" });
    expect(s).toMatchObject({ key: "key-2", keyStatus: "fresh", attempts: [], lastOutcome: null, boundRequest: null });
  });
});
