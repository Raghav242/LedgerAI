import { describe, expect, it, vi } from "vitest";
import { apiError, json, mockApi } from "@/test/mockApi";
import { ApiError, buildUrl, request } from "./client";

async function captureError(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise;
  } catch (error) {
    expect(error).toBeInstanceOf(ApiError);
    return error as ApiError;
  }
  throw new Error("expected the request to fail");
}

describe("api client", () => {
  it("builds same-origin URLs and drops empty query values", () => {
    expect(buildUrl("/api/v1/transfers", { limit: 25, account_id: undefined, kind: "", offset: 0 })).toBe("/api/v1/transfers?limit=25&offset=0");
  });

  it("returns data and parses lab-relevant response headers", async () => {
    mockApi({
      "GET /api/v1/thing": () =>
        json(
          { ok: true },
          {
            headers: {
              "x-request-id": "req-1",
              "x-served-by": "api-2",
              "x-ratelimit-policy": "read",
              "x-ratelimit-limit": "100",
              "x-ratelimit-remaining": "99",
              "idempotent-replayed": "true",
            },
          },
        ),
    });
    const { data, meta } = await request<{ ok: boolean }>("GET", "/api/v1/thing");
    expect(data).toEqual({ ok: true });
    expect(meta).toMatchObject({
      status: 200,
      requestId: "req-1",
      servedBy: "api-2",
      rateLimit: { policy: "read", limit: 100, remaining: 99 },
      idempotentReplayed: true,
      retryAfterSeconds: null,
    });
  });

  it("sends JSON bodies and custom headers", async () => {
    const api = mockApi({ "POST /api/v1/thing": () => json({}, { status: 201 }) });
    await request("POST", "/api/v1/thing", { body: { amount_minor: 1 }, headers: { "Idempotency-Key": "k1" } });
    expect(api.requests[0]).toMatchObject({ body: { amount_minor: 1 }, headers: { "idempotency-key": "k1", "content-type": "application/json" } });
  });

  it("normalizes the backend error envelope", async () => {
    mockApi({
      "POST /api/v1/transfers": () =>
        apiError(409, "INSUFFICIENT_FUNDS", "Source account has insufficient funds", { balance_minor: 1 }),
    });
    const error = await captureError(request("POST", "/api/v1/transfers", { body: {} }));
    expect(error).toMatchObject({ kind: "http", status: 409, code: "INSUFFICIENT_FUNDS", details: { balance_minor: 1 } });
    expect(error.message).toBe("Source account has insufficient funds");
  });

  it("exposes Retry-After on 429", async () => {
    mockApi({ "POST /api/v1/transfers": () => apiError(429, "RATE_LIMITED", "Rate limit exceeded; retry later", null, { "retry-after": "3" }) });
    const error = await captureError(request("POST", "/api/v1/transfers"));
    expect(error.status).toBe(429);
    expect(error.retryAfterSeconds).toBe(3);
  });

  it("maps a non-JSON gateway error to a synthetic code", async () => {
    mockApi({ "GET /api/v1/thing": () => new Response("<html>502 Bad Gateway</html>", { status: 502, headers: { "content-type": "text/html" } }) });
    const error = await captureError(request("GET", "/api/v1/thing"));
    expect(error).toMatchObject({ kind: "http", status: 502, code: "HTTP_502" });
    expect(error.message).toMatch(/load balancer/);
  });

  it("reports a network failure without a status", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    const error = await captureError(request("GET", "/api/v1/thing"));
    expect(error).toMatchObject({ kind: "network", status: null, code: "NETWORK_ERROR" });
  });

  it("times out a request that never answers", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_url: string, init: RequestInit) =>
          new Promise((_resolve, reject) => {
            init.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
          }),
      ),
    );
    const error = await captureError(request("GET", "/api/v1/slow", { timeoutMs: 20 }));
    expect(error).toMatchObject({ kind: "timeout", code: "TIMEOUT", status: null });
  });

  it("distinguishes caller cancellation from a timeout", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_url: string, init: RequestInit) =>
          new Promise((_resolve, reject) => {
            init.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
          }),
      ),
    );
    const controller = new AbortController();
    const pending = request("GET", "/api/v1/slow", { signal: controller.signal, timeoutMs: 5_000 });
    controller.abort();
    const error = await captureError(pending);
    expect(error.kind).toBe("aborted");
  });
});
