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
  gap_vs_typical: "Gap vs this account's usual pace",
  burst_10s: "Transfers in the previous 10 s",
  rate_vs_typical_60s: "Last-minute rate vs usual",
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
    case "gap_vs_typical": {
      if (v === 0) return "no cadence yet";
      const r = Math.exp(v);
      return r >= 1 ? `${r.toFixed(1)}x slower` : `${(1 / r).toFixed(1)}x faster`;
    }
    case "burst_10s":
      return String(Math.round(v));
    case "rate_vs_typical_60s":
      return v === 0 ? "no cadence yet" : `${Math.exp(v).toFixed(1)}x usual`;
    default:
      return v.toFixed(3);
  }
}

/**
 * Where a score sits on the bar (0..1). iforest-v1 scores are raw isolation scores
 * (~0.3-0.8); iforest-v2 scores are training percentiles in [0, 1] with thresholds near
 * 0.998, shown on a "how rare" scale: 0.9 -> 1 in 10, 0.99 -> 1 in 100, 0.999 -> 1 in 1000.
 */
export function scorePosition(version: string, score: number): number {
  if (version === "iforest-v1") return Math.min(1, Math.max(0, (score - 0.3) / 0.5));
  const rarity = -Math.log10(Math.max(1e-4, 1 - score)); // 0 .. 4
  return Math.min(1, Math.max(0, rarity / 4));
}

/** Plain-language reading of a score (never a probability). */
export function scoreReading(version: string, score: number): string {
  if (version === "iforest-v1") return `isolation score ${score.toFixed(3)}`;
  const pct = score * 100;
  return `more unusual than ${pct >= 99.9 ? pct.toFixed(2) : pct.toFixed(1)}% of training transfers in at least one respect`;
}

export type Verdicts = { model: boolean; rules: boolean; final: boolean };

/** The model's own verdict, the rule baseline's, and the stored final decision. */
export function verdicts(r: { score: number; threshold: number; rule_flags: string[]; is_anomaly: boolean }): Verdicts {
  return { model: r.score >= r.threshold, rules: r.rule_flags.length > 0, final: r.is_anomaly };
}
