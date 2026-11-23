/**
 * Presentation helpers for the anomaly model. Features are stored on log scales
 * (app/ai/features.py); these turn them back into quantities people recognise.
 */
export const SCENARIOS = [
  { index: 0, id: "normal", name: "Normal transaction", hint: "Six ~$30 transfers, then another ~$30", intent: "usual" },
  { index: 1, id: "large_amount", name: "Unusually large", hint: "Same habit, then $900 (~30x)", intent: "unusual" },
  { index: 2, id: "rapid_burst", name: "Rapid burst", hint: "Same habit, then six back to back", intent: "unusual" },
  { index: 3, id: "new_account", name: "New-account activity", hint: "A brand-new account sends $600 first", intent: "unusual" },
] as const;

export type ScenarioId = (typeof SCENARIOS)[number]["id"];

const SINCE_OFFSET = 0.1;

export const FEATURE_LABEL: Record<string, string> = {
  log_amount: "Amount",
  amount_vs_baseline: "Amount vs this account's usual",
  log_prior_transfers: "Earlier transfers from this account",
  burst_60s: "Transfers in the previous 60 s",
  log_secs_since_prev: "Time since previous transfer",
  log_account_age: "Account age",
};

/** Human-readable value of one (log-scale) feature. */
export function featureValue(name: string, v: number): string {
  switch (name) {
    case "log_amount":
      return `$${Math.expm1(v).toLocaleString(undefined, { maximumFractionDigits: 2, minimumFractionDigits: 2 })}`;
    case "amount_vs_baseline": {
      const r = Math.exp(v);
      return v === 0 ? "no history" : `${r >= 1 ? r.toFixed(1) : (1 / r).toFixed(1)}x ${r >= 1 ? "larger" : "smaller"}`;
    }
    case "log_prior_transfers":
      return String(Math.round(Math.expm1(v)));
    case "burst_60s":
      return String(Math.round(v));
    case "log_secs_since_prev":
      return `${Math.max(0, Math.exp(v) - SINCE_OFFSET).toFixed(1)} s`;
    case "log_account_age": {
      const s = Math.expm1(v);
      return s < 120 ? `${s.toFixed(0)} s` : `${(s / 60).toFixed(0)} min`;
    }
    default:
      return v.toFixed(3);
  }
}
