import type { LedgerEntry, Page, Transfer, TransferCreate, TransferKind } from "@/types/api";
import { API_V1, http, type ApiResponse } from "./client";

export interface TransferListParams {
  account_id?: string;
  kind?: TransferKind;
  limit?: number;
  offset?: number;
}

export const transfersApi = {
  list: async (params: TransferListParams = {}, signal?: AbortSignal) =>
    (await http.get<Page<Transfer>>(`${API_V1}/transfers`, { query: { ...params }, signal })).data,

  get: async (id: string, signal?: AbortSignal) =>
    (await http.get<Transfer>(`${API_V1}/transfers/${encodeURIComponent(id)}`, { signal })).data,

  ledgerEntries: async (id: string, signal?: AbortSignal) =>
    (await http.get<LedgerEntry[]>(`${API_V1}/transfers/${encodeURIComponent(id)}/ledger-entries`, { signal })).data,

  /**
   * Returns the full response (status + headers), not just the body: the playground shows
   * the HTTP status, serving replica and Idempotent-Replayed flag.
   * The caller owns the Idempotency-Key; this function never invents one.
   */
  create: (payload: TransferCreate, idempotencyKey: string): Promise<ApiResponse<Transfer>> =>
    http.post<Transfer>(`${API_V1}/transfers`, payload, { headers: { "Idempotency-Key": idempotencyKey } }),
};
