import type { components } from "@/types/openapi.gen";
import { API_V1, http } from "./client";

export type ModelInfo = components["schemas"]["ModelInfo"];
export type AnomalyResult = components["schemas"]["AnomalyResultRead"];
export type DriftReport = components["schemas"]["DriftReport"];

/** Read-only AI endpoints: the model card, persisted results and input drift. */
export const aiApi = {
  model: async (signal?: AbortSignal) => (await http.get<ModelInfo>(`${API_V1}/ai/model`, { signal })).data,
  drift: async (signal?: AbortSignal) => (await http.get<DriftReport>(`${API_V1}/ai/drift`, { signal })).data,
  results: async (limit = 10, signal?: AbortSignal) =>
    (await http.get<AnomalyResult[]>(`${API_V1}/ai/results`, { query: { limit }, signal })).data,
};
