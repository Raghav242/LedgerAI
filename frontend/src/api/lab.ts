import type { ExperimentDefinition, ExperimentId, LabEventPage, LabRun, LabRunSummary } from "@/types/api";
import { API_V1, http } from "./client";

/** Experiment Lab (Phase 5B): allow-listed experiments, background runs, incremental events. */
export const labApi = {
  experiments: async (signal?: AbortSignal) =>
    (await http.get<ExperimentDefinition[]>(`${API_V1}/lab/experiments`, { signal })).data,

  runs: async (limit = 20, signal?: AbortSignal) =>
    (await http.get<LabRunSummary[]>(`${API_V1}/lab/runs`, { query: { limit }, signal })).data,

  run: async (id: string, signal?: AbortSignal) =>
    (await http.get<LabRun>(`${API_V1}/lab/runs/${encodeURIComponent(id)}`, { signal })).data,

  events: async (id: string, after: number, limit = 500, signal?: AbortSignal) =>
    (
      await http.get<LabEventPage>(`${API_V1}/lab/runs/${encodeURIComponent(id)}/events`, {
        query: { after, limit },
        signal,
      })
    ).data,

  start: async (experiment: ExperimentId, params: Record<string, number>) =>
    (await http.post<LabRun>(`${API_V1}/lab/runs`, { experiment, params })).data,
};
