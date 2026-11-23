import type { EventLifecycle, EventPage, PipelineSummary, TopologySnapshot, TraceView } from "@/types/api";
import { API_V1, http } from "./client";

export type EventStatusFilter = "all" | "unpublished" | "published" | "retrying";

export interface EventListParams {
  status: EventStatusFilter;
  event_type?: "transfer.completed" | "account.funded";
  correlation_id?: string;
  limit: number;
  cursor?: string;
}

/** Read-only observability endpoints (Phase 5C). Nothing here changes system state. */
export const observabilityApi = {
  topology: async (signal?: AbortSignal) =>
    (await http.get<TopologySnapshot>(`${API_V1}/observability/topology`, { signal, timeoutMs: 8_000 })).data,

  pipelineSummary: async (signal?: AbortSignal) =>
    (await http.get<PipelineSummary>(`${API_V1}/observability/events/summary`, { signal })).data,

  events: async (params: EventListParams, signal?: AbortSignal) =>
    (await http.get<EventPage>(`${API_V1}/observability/events`, { query: { ...params }, signal })).data,

  eventLifecycle: async (eventId: string, signal?: AbortSignal) =>
    (await http.get<EventLifecycle>(`${API_V1}/observability/events/${encodeURIComponent(eventId)}`, { signal })).data,

  transferLifecycle: async (transferId: string, signal?: AbortSignal) =>
    (await http.get<EventLifecycle>(`${API_V1}/observability/transfers/${encodeURIComponent(transferId)}/lifecycle`, { signal })).data,

  trace: async (traceId: string, signal?: AbortSignal) =>
    (await http.get<TraceView>(`${API_V1}/observability/traces/${encodeURIComponent(traceId)}`, { signal })).data,
};
