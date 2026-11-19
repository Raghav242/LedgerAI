import type { Account, AccountCreate, Page } from "@/types/api";
import { API_V1, http } from "./client";

/** Largest page the backend allows (LimitQuery le=100). */
export const MAX_PAGE_SIZE = 100;

export const accountsApi = {
  list: async (params: { limit?: number; offset?: number; order?: "asc" | "desc" } = {}, signal?: AbortSignal) =>
    (await http.get<Page<Account>>(`${API_V1}/accounts`, { query: params, signal })).data,

  get: async (id: string, signal?: AbortSignal) =>
    (await http.get<Account>(`${API_V1}/accounts/${encodeURIComponent(id)}`, { signal })).data,

  create: async (payload: AccountCreate) => (await http.post<Account>(`${API_V1}/accounts`, payload)).data,

  /** DEVELOPMENT ONLY: exists only when the backend runs with ENABLE_DEV_ENDPOINTS=true. */
  fund: async (id: string, amountMinor: number) =>
    (await http.post<Account>(`${API_V1}/accounts/${encodeURIComponent(id)}/fund`, { amount_minor: amountMinor })).data,
};
