import { QueryClient } from "@tanstack/react-query";
import { isApiError } from "@/api/client";

/**
 * Server state lives in TanStack Query; UI state stays in components. Defaults are tuned for
 * a rate-limited API: one retry only for transient failures (never for 4xx such as 429, and
 * not for 503, which here means "fail closed"), no refetch on window focus, short staleness.
 */
export function createQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 5_000,
        refetchOnWindowFocus: false,
        retry: (failureCount, error) => {
          if (failureCount >= 1 || !isApiError(error)) return false;
          if (error.kind === "network" || error.kind === "timeout") return true;
          return error.status !== null && error.status >= 500 && error.status !== 503;
        },
      },
      // Money-moving requests are only ever retried by an explicit user action.
      mutations: { retry: false },
    },
  });
}
