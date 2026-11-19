import type { ReadinessReport } from "@/types/api";
import { ApiError, http, type ResponseMeta } from "./client";

export interface ReadinessResult {
  report: ReadinessReport;
  meta: ResponseMeta | null;
}

/**
 * /health/ready answers 503 with a JSON body when a dependency is down. That is a valid
 * *report* (degraded), not a failed request, so it is returned rather than thrown.
 */
export async function fetchReadiness(signal?: AbortSignal): Promise<ReadinessResult> {
  try {
    const { data, meta } = await http.get<ReadinessReport>("/health/ready", { signal, timeoutMs: 5_000 });
    return { report: data, meta };
  } catch (error) {
    if (error instanceof ApiError && error.status === 503 && isReport(error.details)) {
      return { report: error.details, meta: error.meta };
    }
    throw error;
  }
}

function isReport(value: unknown): value is ReadinessReport {
  return typeof value === "object" && value !== null && "status" in value;
}
