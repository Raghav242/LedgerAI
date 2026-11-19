import { useMemo } from "react";
import { SYSTEM_FUNDING_ACCOUNT_ID } from "@/lib/ids";
import { shortId } from "@/lib/format";
import type { Account } from "@/types/api";
import { useAccounts } from "./queries";

export interface AccountDirectory {
  accounts: Account[];
  byId: Map<string, Account>;
  /** Display name for an account id; falls back to a short id for accounts not loaded. */
  nameOf: (id: string) => string;
  isLoading: boolean;
}

/** Resolves account ids to names from the (bounded) account list already in the cache. */
export function useAccountDirectory(): AccountDirectory {
  const query = useAccounts();
  return useMemo(() => {
    const accounts = query.data?.items ?? [];
    const byId = new Map(accounts.map((a) => [a.id, a]));
    const nameOf = (id: string) => {
      if (id === SYSTEM_FUNDING_ACCOUNT_ID) return "System funding";
      return byId.get(id)?.name ?? shortId(id);
    };
    return { accounts, byId, nameOf, isLoading: query.isLoading };
  }, [query.data, query.isLoading]);
}
