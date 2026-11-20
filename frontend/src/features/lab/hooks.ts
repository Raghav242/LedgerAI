import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { labApi } from "@/api/lab";
import { queryKeys } from "@/api/queryKeys";
import { invalidateAfterMoneyMovement } from "@/hooks/queries";
import type { ExperimentId, LabEvent, LabRun, LabRunStatus } from "@/types/api";

export const RUN_POLL_MS = 800;
const EVENTS_PAGE = 500;

export function isActive(status: LabRunStatus | undefined): boolean {
  return status === "queued" || status === "running";
}

/** The catalog never changes while the backend runs. */
export function useExperiments() {
  return useQuery({
    queryKey: queryKeys.lab.experiments,
    queryFn: ({ signal }) => labApi.experiments(signal),
    staleTime: Infinity,
  });
}

export function useLabRuns() {
  return useQuery({
    queryKey: queryKeys.lab.runs,
    queryFn: ({ signal }) => labApi.runs(20, signal),
    // Keep the list fresh while some run is still in flight.
    refetchInterval: (query) => (query.state.data?.some((r) => isActive(r.status)) ? 2_000 : false),
  });
}

/**
 * One run, polled while it is queued or running. When it finishes, everything the
 * experiment touched (accounts, transfers, ledger, reconciliation, summary) is refreshed.
 */
export function useLabRun(id: string | null) {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: queryKeys.lab.run(id ?? ""),
    queryFn: ({ signal }) => labApi.run(id!, signal),
    enabled: !!id,
    refetchInterval: (q) => (q.state.data && !isActive(q.state.data.status) ? false : RUN_POLL_MS),
  });
  const status = query.data?.status;
  const previous = useRef<LabRunStatus | undefined>(undefined);
  useEffect(() => {
    if (previous.current && isActive(previous.current) && status && !isActive(status)) {
      void queryClient.invalidateQueries({ queryKey: queryKeys.lab.runs });
      void invalidateAfterMoneyMovement(queryClient);
    }
    previous.current = status;
  }, [status, queryClient]);
  return query;
}

export interface RunEventsState {
  events: LabEvent[];
  after: number;
  runStatus: LabRunStatus;
  eventCount: number;
  dropped: number;
}

/**
 * The run's timeline, fetched incrementally (`after` = last seen sequence number) and
 * accumulated in the query cache, so each poll transfers only new events.
 */
export function useRunEvents(id: string | null) {
  const queryClient = useQueryClient();
  const key = queryKeys.lab.events(id ?? "");
  return useQuery({
    queryKey: key,
    enabled: !!id,
    queryFn: async ({ signal }): Promise<RunEventsState> => {
      const previous = queryClient.getQueryData<RunEventsState>(key);
      let events = previous?.events ?? [];
      let after = previous?.after ?? 0;
      let page = await labApi.events(id!, after, EVENTS_PAGE, signal);
      events = events.concat(page.items);
      after = page.next_after;
      // Drain a backlog (e.g. opening a finished run) without waiting for the next poll.
      for (let i = 0; i < 4 && page.items.length === EVENTS_PAGE; i++) {
        page = await labApi.events(id!, after, EVENTS_PAGE, signal);
        events = events.concat(page.items);
        after = page.next_after;
      }
      return { events, after, runStatus: page.run_status, eventCount: page.event_count, dropped: page.events_dropped };
    },
    refetchInterval: (q) => {
      const data = q.state.data;
      if (!data) return RUN_POLL_MS;
      return !isActive(data.runStatus) && data.after >= data.eventCount ? false : RUN_POLL_MS;
    },
  });
}

export function useStartRun() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ experiment, params }: { experiment: ExperimentId; params: Record<string, number> }) =>
      labApi.start(experiment, params),
    onSuccess: (run: LabRun) => {
      queryClient.setQueryData(queryKeys.lab.run(run.id), run);
      void queryClient.invalidateQueries({ queryKey: queryKeys.lab.runs });
    },
  });
}
