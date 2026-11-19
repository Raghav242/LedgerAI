import { isApiError } from "@/api/client";

/** Human-readable description of any thrown value, with the backend's error code when present. */
export function describeError(error: unknown): { title: string; message: string; code: string | null; status: number | null } {
  if (isApiError(error)) {
    if (error.kind === "network") {
      return { title: "API unreachable", message: error.message, code: error.code, status: null };
    }
    if (error.kind === "timeout") {
      return { title: "Request timed out", message: error.message, code: error.code, status: null };
    }
    if (error.status === 429) {
      const wait = error.retryAfterSeconds;
      return {
        title: "Rate limited",
        message: `The shared read quota is used up.${wait !== null ? ` Retry in ${wait} s.` : ""}`,
        code: error.code,
        status: 429,
      };
    }
    return { title: `Request failed (${error.status})`, message: error.message, code: error.code, status: error.status };
  }
  return { title: "Unexpected error", message: error instanceof Error ? error.message : String(error), code: null, status: null };
}
