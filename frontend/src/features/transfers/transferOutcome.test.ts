import { describe, expect, it } from "vitest";
import { ApiError, type ResponseMeta } from "@/api/client";
import { mustReuseKey, outcomeFromError, outcomeFromSuccess } from "./transferOutcome";

function meta(overrides: Partial<ResponseMeta> = {}): ResponseMeta {
  return {
    status: 201,
    requestId: "r",
    servedBy: "api-1",
    rateLimit: { policy: "write", limit: 20, remaining: 19 },
    retryAfterSeconds: null,
    idempotentReplayed: false,
    durationMs: 12,
    ...overrides,
  };
}

function httpError(status: number, code: string, metaOverrides: Partial<ResponseMeta> = {}) {
  return new ApiError({ kind: "http", status, code, message: code, meta: meta({ status, ...metaOverrides }) });
}

const transfer = {
  id: "t1",
  source_account_id: "a",
  destination_account_id: "b",
  amount_minor: 100,
  currency: "USD" as const,
  status: "completed" as const,
  kind: "transfer" as const,
  created_at: "2026-11-18T00:00:00Z",
};

describe("transfer outcome classification", () => {
  it("201 is a success", () => {
    const o = outcomeFromSuccess({ data: transfer, meta: meta() });
    expect(o).toMatchObject({ category: "succeeded", status: 201, replayed: false, transfer });
  });

  it("a replayed 201 is a success flagged as replay", () => {
    const o = outcomeFromSuccess({ data: transfer, meta: meta({ idempotentReplayed: true }) });
    expect(o).toMatchObject({ category: "succeeded", replayed: true, title: "Replayed original success" });
  });

  it.each([
    [409, "INSUFFICIENT_FUNDS"],
    [404, "ACCOUNT_NOT_FOUND"],
    [422, "SAME_ACCOUNT_TRANSFER"],
    [422, "CURRENCY_MISMATCH"],
  ])("%d %s is a final rejection", (status, code) => {
    const o = outcomeFromError(httpError(status, code));
    expect(o.category).toBe("rejected");
    expect(o.guidance).toMatch(/final/);
    expect(mustReuseKey(o.category)).toBe(false);
  });

  it("insufficient funds has a readable title", () => {
    expect(outcomeFromError(httpError(409, "INSUFFICIENT_FUNDS")).title).toBe("Insufficient funds (409)");
  });

  it("key reuse and schema validation are invalid requests", () => {
    expect(outcomeFromError(httpError(422, "IDEMPOTENCY_KEY_REUSED")).category).toBe("invalid");
    expect(outcomeFromError(httpError(422, "VALIDATION_ERROR")).category).toBe("invalid");
  });

  it.each([
    [429, "RATE_LIMITED"],
    [503, "RATE_LIMITER_UNAVAILABLE"],
    [503, "CONCURRENCY_CONFLICT"],
    [409, "IDEMPOTENCY_REQUEST_IN_PROGRESS"],
  ])("%d %s is retryable with the same key", (status, code) => {
    const o = outcomeFromError(httpError(status, code));
    expect(o.category).toBe("retryable");
    expect(mustReuseKey(o.category)).toBe(true);
    expect(o.guidance).toMatch(/same key/);
  });

  it("429 carries Retry-After", () => {
    const o = outcomeFromError(httpError(429, "RATE_LIMITED", { retryAfterSeconds: 4 }));
    expect(o.retryAfterSeconds).toBe(4);
  });

  it("503 limiter outage explains fail-closed", () => {
    const o = outcomeFromError(httpError(503, "RATE_LIMITER_UNAVAILABLE"));
    expect(o.title).toBe("Rate limiter unavailable (503)");
    expect(o.guidance).toMatch(/fail closed/);
  });

  it.each([
    httpError(500, "OUTCOME_UNKNOWN"),
    httpError(500, "INTERNAL_ERROR"),
    httpError(502, "HTTP_502"),
    httpError(504, "HTTP_504"),
    new ApiError({ kind: "timeout", status: null, code: "TIMEOUT", message: "t" }),
    new ApiError({ kind: "network", status: null, code: "NETWORK_ERROR", message: "n" }),
    new TypeError("boom"),
  ])("uncertain outcome %# is ambiguous and requires the same key", (error) => {
    const o = outcomeFromError(error);
    expect(o.category).toBe("ambiguous");
    expect(mustReuseKey(o.category)).toBe(true);
    expect(o.guidance).toMatch(/SAME idempotency key/);
  });
});
