/**
 * The single HTTP entry point. Every request goes through `request()`, which:
 *  - resolves paths against one configured base URL (same-origin by default),
 *  - enforces a timeout (AbortController) and honours caller cancellation,
 *  - parses JSON and normalizes every failure into an ApiError,
 *  - surfaces the response headers the lab cares about (replica, request id, quota).
 *
 * UI code never calls fetch() directly and never builds absolute URLs.
 */
import type { ErrorBody } from "@/types/api";

const env = import.meta.env;
export const API_BASE_URL: string = (env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");
export const DEFAULT_TIMEOUT_MS: number = Number(env.VITE_API_TIMEOUT_MS) > 0 ? Number(env.VITE_API_TIMEOUT_MS) : 10_000;
export const API_V1 = "/api/v1";

export interface RateLimitInfo {
  policy: string | null;
  limit: number | null;
  remaining: number | null;
}

/** Metadata parsed from response headers (all optional: NGINX errors carry none of them). */
export interface ResponseMeta {
  status: number;
  requestId: string | null;
  servedBy: string | null;
  rateLimit: RateLimitInfo;
  retryAfterSeconds: number | null;
  idempotentReplayed: boolean;
  durationMs: number;
}

export interface ApiResponse<T> {
  data: T;
  meta: ResponseMeta;
}

export type ApiErrorKind =
  | "http" // the server answered with a non-2xx status
  | "network" // no response at all (connection refused, DNS, CORS, proxy down)
  | "timeout" // no response within the client timeout
  | "aborted"; // the caller cancelled (e.g. component unmounted)

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  /** HTTP status, or null when no response was received. */
  readonly status: number | null;
  /** Backend error code (e.g. INSUFFICIENT_FUNDS) or a synthetic one (NETWORK_ERROR, HTTP_502). */
  readonly code: string;
  readonly details: unknown;
  readonly meta: ResponseMeta | null;

  constructor(init: {
    kind: ApiErrorKind;
    status: number | null;
    code: string;
    message: string;
    details?: unknown;
    meta?: ResponseMeta | null;
  }) {
    super(init.message);
    this.name = "ApiError";
    this.kind = init.kind;
    this.status = init.status;
    this.code = init.code;
    this.details = init.details ?? null;
    this.meta = init.meta ?? null;
  }

  get retryAfterSeconds(): number | null {
    return this.meta?.retryAfterSeconds ?? null;
  }
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}

type QueryValue = string | number | boolean | null | undefined;

export interface RequestOptions {
  query?: Record<string, QueryValue>;
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  timeoutMs?: number;
}

export function buildUrl(path: string, query?: Record<string, QueryValue>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  }
  const qs = params.toString();
  return `${API_BASE_URL}${path}${qs ? `?${qs}` : ""}`;
}

function intHeader(headers: Headers, name: string): number | null {
  const raw = headers.get(name);
  if (raw === null || raw.trim() === "") return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}

function readMeta(response: Response, startedAt: number): ResponseMeta {
  const h = response.headers;
  return {
    status: response.status,
    requestId: h.get("x-request-id"),
    servedBy: h.get("x-served-by"),
    rateLimit: {
      policy: h.get("x-ratelimit-policy"),
      limit: intHeader(h, "x-ratelimit-limit"),
      remaining: intHeader(h, "x-ratelimit-remaining"),
    },
    retryAfterSeconds: intHeader(h, "retry-after"),
    idempotentReplayed: h.get("idempotent-replayed") === "true",
    durationMs: Math.round(performance.now() - startedAt),
  };
}

const STATUS_TEXT: Record<number, string> = {
  500: "Internal server error",
  502: "Bad gateway: the load balancer could not get a response from an API replica",
  503: "Service unavailable",
  504: "Gateway timeout: no API replica answered in time",
};

function isErrorEnvelope(value: unknown): value is { error: ErrorBody } {
  if (typeof value !== "object" || value === null || !("error" in value)) return false;
  const err = (value as { error: unknown }).error;
  return typeof err === "object" && err !== null && typeof (err as ErrorBody).code === "string";
}

async function parseBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (text === "") return null;
  try {
    return JSON.parse(text);
  } catch {
    return text; // e.g. an HTML error page from the gateway
  }
}

export async function request<T>(method: string, path: string, options: RequestOptions = {}): Promise<ApiResponse<T>> {
  const controller = new AbortController();
  let timedOut = false;
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  const onCallerAbort = () => controller.abort();
  if (options.signal?.aborted) controller.abort();
  options.signal?.addEventListener("abort", onCallerAbort, { once: true });

  const headers: Record<string, string> = { Accept: "application/json", ...options.headers };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";

  const startedAt = performance.now();
  let response: Response;
  let body: unknown;
  try {
    response = await fetch(buildUrl(path, options.query), {
      method,
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: controller.signal,
      credentials: "same-origin",
    });
    body = await parseBody(response);
  } catch (cause) {
    if (timedOut) {
      throw new ApiError({
        kind: "timeout",
        status: null,
        code: "TIMEOUT",
        message: `No response within ${(timeoutMs / 1000).toFixed(0)} s`,
      });
    }
    if (controller.signal.aborted) {
      throw new ApiError({ kind: "aborted", status: null, code: "ABORTED", message: "Request cancelled" });
    }
    throw new ApiError({
      kind: "network",
      status: null,
      code: "NETWORK_ERROR",
      message: "Could not reach the API. Is the backend running?",
      details: cause instanceof Error ? cause.message : String(cause),
    });
  } finally {
    clearTimeout(timer);
    options.signal?.removeEventListener("abort", onCallerAbort);
  }

  const meta = readMeta(response, startedAt);
  if (response.ok) return { data: body as T, meta };

  if (isErrorEnvelope(body)) {
    throw new ApiError({
      kind: "http",
      status: response.status,
      code: body.error.code,
      message: body.error.message,
      details: body.error.details,
      meta,
    });
  }
  throw new ApiError({
    kind: "http",
    status: response.status,
    code: `HTTP_${response.status}`,
    message: STATUS_TEXT[response.status] ?? (response.statusText || `HTTP ${response.status}`),
    details: typeof body === "string" ? body.slice(0, 500) : body,
    meta,
  });
}

export const http = {
  get: <T>(path: string, options?: Omit<RequestOptions, "body">) => request<T>("GET", path, options),
  post: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, "body">) =>
    request<T>("POST", path, { ...options, body }),
};
