import type { LedgerEntry, Page, Reconciliation, SystemSummary } from "@/types/api";
import { API_V1, http } from "./client";

export const ledgerApi = {
  entries: async (params: { account_id?: string; limit?: number; offset?: number } = {}, signal?: AbortSignal) =>
    (await http.get<Page<LedgerEntry>>(`${API_V1}/ledger/entries`, { query: params, signal })).data,
};

export const reconciliationApi = {
  run: async (signal?: AbortSignal) => (await http.get<Reconciliation>(`${API_V1}/reconciliation`, { signal })).data,
};

export const statsApi = {
  summary: async (activityDays = 14, signal?: AbortSignal) =>
    (await http.get<SystemSummary>(`${API_V1}/stats/summary`, { query: { activity_days: activityDays }, signal })).data,
};
