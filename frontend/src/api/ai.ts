import type { components } from "@/types/openapi.gen";
import { API_V1, http } from "./client";

export type ModelInfo = components["schemas"]["ModelInfo"];
export type AnomalyResult = components["schemas"]["AnomalyResultRead"];

/** Read-only AI endpoints: the model card and the results the AI worker persisted. */
export const aiApi = {
  model: async (signal?: AbortSignal) => (await http.get<ModelInfo>(`${API_V1}/ai/model`, { signal })).data,
  results: async (limit = 10, signal?: AbortSignal) =>
    (await http.get<AnomalyResult[]>(`${API_V1}/ai/results`, { query: { limit }, signal })).data,
};
