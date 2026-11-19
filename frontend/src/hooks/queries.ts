import { keepPreviousData, useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { accountsApi, MAX_PAGE_SIZE } from "@/api/accounts";
import { fetchReadiness } from "@/api/health";
import { ledgerApi, reconciliationApi, statsApi } from "@/api/ledger";
import { queryKeys } from "@/api/queryKeys";
import { transfersApi, type TransferListParams } from "@/api/transfers";
import type { AccountCreate } from "@/types/api";

/**
 * Every committed money movement changes balances, history, ledger, reconciliation inputs
 * and the summary. Invalidate all of them, and only after a *confirmed* success.
 */
export function invalidateAfterMoneyMovement(queryClient: QueryClient): Promise<void> {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: queryKeys.accounts.all }),
    queryClient.invalidateQueries({ queryKey: queryKeys.transfers.all }),
    queryClient.invalidateQueries({ queryKey: queryKeys.ledger.all }),
    queryClient.invalidateQueries({ queryKey: queryKeys.reconciliation }),
    queryClient.invalidateQueries({ queryKey: queryKeys.summaryAll }),
  ]).then(() => undefined);
}

// --- accounts -------------------------------------------------------------------------

/**
 * Pickers and the account list show the newest MAX_PAGE_SIZE merchant accounts (one
 * bounded request, newest first so a just-created account is always included). The backend
 * has no search endpoint yet; see docs/phase-5a-design.md.
 */
export function useAccounts(limit = MAX_PAGE_SIZE, offset = 0) {
  return useQuery({
    queryKey: queryKeys.accounts.list(limit, offset),
    queryFn: ({ signal }) => accountsApi.list({ limit, offset, order: "desc" }, signal),
    placeholderData: keepPreviousData,
  });
}

export function useAccount(id: string | null) {
  return useQuery({
    queryKey: queryKeys.accounts.detail(id ?? ""),
    queryFn: ({ signal }) => accountsApi.get(id!, signal),
    enabled: !!id,
  });
}

export function useCreateAccount() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: AccountCreate) => accountsApi.create(payload),
    onSuccess: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.accounts.all }),
        queryClient.invalidateQueries({ queryKey: queryKeys.summaryAll }),
      ]),
  });
}

export function useFundAccount() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ accountId, amountMinor }: { accountId: string; amountMinor: number }) =>
      accountsApi.fund(accountId, amountMinor),
    onSuccess: () => invalidateAfterMoneyMovement(queryClient),
  });
}

// --- transfers ------------------------------------------------------------------------

export function useTransfers(params: TransferListParams) {
  return useQuery({
    queryKey: queryKeys.transfers.list(params),
    queryFn: ({ signal }) => transfersApi.list(params, signal),
    placeholderData: keepPreviousData,
  });
}

export function useTransfer(id: string | null) {
  return useQuery({
    queryKey: queryKeys.transfers.detail(id ?? ""),
    queryFn: ({ signal }) => transfersApi.get(id!, signal),
    enabled: !!id,
    staleTime: Infinity, // transfers are immutable
  });
}

export function useTransferLedgerEntries(id: string | null) {
  return useQuery({
    queryKey: queryKeys.transfers.ledger(id ?? ""),
    queryFn: ({ signal }) => transfersApi.ledgerEntries(id!, signal),
    enabled: !!id,
    staleTime: Infinity, // ledger entries are append-only and never change
  });
}

// --- ledger, reconciliation, summary, health ---------------------------------------------

export function useLedgerEntries(params: { account_id?: string; limit: number; offset: number }) {
  return useQuery({
    queryKey: queryKeys.ledger.entries(params),
    queryFn: ({ signal }) => ledgerApi.entries(params, signal),
    placeholderData: keepPreviousData,
  });
}

export function useReconciliation() {
  return useQuery({
    queryKey: queryKeys.reconciliation,
    queryFn: ({ signal }) => reconciliationApi.run(signal),
    // A stale "passed" must never be presented as current: always re-run on mount.
    staleTime: 0,
  });
}

export function useSummary(days = 14) {
  return useQuery({
    queryKey: queryKeys.summary(days),
    queryFn: ({ signal }) => statsApi.summary(days, signal),
  });
}

/** Polled gently (every 15 s); /health/ready is not rate limited but still costs a DB round trip. */
export function useReadiness() {
  return useQuery({
    queryKey: queryKeys.health,
    queryFn: ({ signal }) => fetchReadiness(signal),
    refetchInterval: 15_000,
    retry: false,
  });
}
