/**
 * Minimal fetch mock for component tests: routes "METHOD /path" to handlers that return
 * real Response objects, and records every request for assertions. No network, no MSW.
 */
import { vi } from "vitest";

export interface RecordedRequest {
  method: string;
  path: string;
  query: URLSearchParams;
  headers: Record<string, string>;
  body: unknown;
}

type Handler = (req: RecordedRequest) => Response | Promise<Response>;

export function json(body: unknown, init: { status?: number; headers?: Record<string, string> } = {}): Response {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { "content-type": "application/json", ...init.headers },
  });
}

export function apiError(status: number, code: string, message: string, details: unknown = null, headers?: Record<string, string>) {
  return json({ error: { code, message, details } }, { status, headers });
}

export function mockApi(routes: Record<string, Handler | Handler[]>) {
  const requests: RecordedRequest[] = [];
  const queues = new Map(Object.entries(routes).map(([k, v]) => [k, Array.isArray(v) ? [...v] : v]));

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const method = (init?.method ?? "GET").toUpperCase();
    const headers: Record<string, string> = {};
    new Headers(init?.headers).forEach((value, key) => (headers[key] = value));
    const req: RecordedRequest = {
      method,
      path: url.pathname,
      query: url.searchParams,
      headers,
      body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
    };
    requests.push(req);

    const entry = queues.get(`${method} ${url.pathname}`) ?? findPattern(queues, method, url.pathname);
    if (!entry) return apiError(404, "NOT_FOUND", `No mock for ${method} ${url.pathname}`);
    // An array is consumed in order (the last handler repeats): models retries.
    const handler = Array.isArray(entry) ? (entry.length > 1 ? entry.shift()! : entry[0]!) : entry;
    if (init?.signal?.aborted) throw new DOMException("Aborted", "AbortError");
    return handler(req);
  });

  vi.stubGlobal("fetch", fetchMock);
  return {
    requests,
    fetchMock,
    calls: (method: string, path: string) => requests.filter((r) => r.method === method && r.path === path),
  };
}

/** Patterns like "GET /api/v1/transfers/:id" match one path segment per parameter. */
function findPattern(queues: Map<string, Handler | Handler[]>, method: string, path: string) {
  for (const [key, value] of queues) {
    const [m, pattern] = key.split(" ");
    if (m !== method || !pattern?.includes(":")) continue;
    const re = new RegExp(`^${pattern.replace(/:[^/]+/g, "[^/]+")}$`);
    if (re.test(path)) return value;
  }
  return undefined;
}

// --- fixtures -------------------------------------------------------------------------

export const ALICE = {
  id: "11111111-1111-4111-8111-111111111111",
  name: "Alice Coffee",
  currency: "USD" as const,
  kind: "merchant" as const,
  balance_minor: 10_000,
  created_at: "2026-11-18T10:00:00Z",
};
export const BOB = {
  id: "22222222-2222-4222-8222-222222222222",
  name: "Bob Bakery",
  currency: "USD" as const,
  kind: "merchant" as const,
  balance_minor: 500,
  created_at: "2026-11-18T10:01:00Z",
};

export function accountsPage(items = [ALICE, BOB]) {
  return json({ items, limit: 100, offset: 0 });
}

export function transferBody(overrides: Record<string, unknown> = {}) {
  return {
    id: "33333333-3333-4333-8333-333333333333",
    source_account_id: ALICE.id,
    destination_account_id: BOB.id,
    amount_minor: 2550,
    currency: "USD",
    status: "completed",
    kind: "transfer",
    created_at: "2026-11-18T11:00:00Z",
    ...overrides,
  };
}

export const readyOk = () => json({ status: "ok", replica: "api-1", database: "ok", redis: "ok" });
