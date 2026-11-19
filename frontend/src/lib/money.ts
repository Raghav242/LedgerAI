/**
 * Money helpers. The backend stores and returns integer minor units (cents). The browser
 * never uses floating-point arithmetic to compute an amount: user input is parsed as a
 * decimal *string* into an integer, and display formatting splits an integer with integer
 * operations only. Balances shown are always the server's numbers, never recomputed here.
 */

/** Backend limit per request ($1B in cents); mirrors MAX_AMOUNT_MINOR in app/schemas/common.py. */
export const MAX_AMOUNT_MINOR = 100_000_000_000;

export const SUPPORTED_CURRENCIES = ["USD"] as const;
export type SupportedCurrency = (typeof SUPPORTED_CURRENCIES)[number];

export function isSupportedCurrency(value: string): value is SupportedCurrency {
  return (SUPPORTED_CURRENCIES as readonly string[]).includes(value);
}

export type ParseAmountResult =
  | { ok: true; minor: number }
  | { ok: false; error: string };

// Digits with optional thousands commas, then up to two decimal places. No sign, no exponent.
const AMOUNT_PATTERN = /^(\d{1,3}(,\d{3})*|\d+)(\.(\d{0,2}))?$/;

/**
 * Parse a user-entered USD amount ("25", "25.5", "1,250.75") into integer cents.
 * Pure string -> integer conversion: "0.1" + "0.2" style float errors cannot occur.
 */
export function parseUsdToMinor(input: string): ParseAmountResult {
  const text = input.trim().replace(/^\$/, "");
  if (text === "") return { ok: false, error: "Enter an amount" };
  if (text.startsWith("-")) return { ok: false, error: "Amount must be positive" };
  const match = AMOUNT_PATTERN.exec(text);
  if (!match) {
    return /\.\d{3,}$/.test(text)
      ? { ok: false, error: "Use at most two decimal places (cents)" }
      : { ok: false, error: "Enter a valid amount, e.g. 25.00" };
  }
  const wholeDigits = (match[1] ?? "").replaceAll(",", "").replace(/^0+(?=\d)/, "");
  const centsDigits = (match[4] ?? "").padEnd(2, "0");
  // Reject absurdly long input before building a number that could lose precision.
  if (wholeDigits.length > 12) return { ok: false, error: "Amount exceeds the $1,000,000,000.00 limit" };
  const minor = Number(wholeDigits) * 100 + Number(centsDigits);
  if (!Number.isSafeInteger(minor)) return { ok: false, error: "Amount is too large" };
  if (minor <= 0) return { ok: false, error: "Amount must be greater than $0.00" };
  if (minor > MAX_AMOUNT_MINOR) return { ok: false, error: "Amount exceeds the $1,000,000,000.00 limit" };
  return { ok: true, minor };
}

const groupFormatter = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });

/** Format integer cents as "$1,234.56" (or "-$25.00"). Integer-only arithmetic. */
export function formatMinor(minor: number, options: { signed?: boolean; currency?: string } = {}): string {
  if (!Number.isSafeInteger(minor)) throw new RangeError(`Not a safe integer amount: ${minor}`);
  const negative = minor < 0;
  const abs = Math.abs(minor);
  const cents = abs % 100;
  const whole = (abs - cents) / 100; // exact: abs - cents is a multiple of 100
  const body = `$${groupFormatter.format(whole)}.${String(cents).padStart(2, "0")}`;
  const sign = negative ? "-" : options.signed && minor > 0 ? "+" : "";
  const suffix = options.currency && options.currency !== "USD" ? ` ${options.currency}` : "";
  return `${sign}${body}${suffix}`;
}

/** Compact label for chart axes ("$1.2K"). Display only; never used for amounts. */
export function formatMinorCompact(minor: number): string {
  const dollars = minor / 100;
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(dollars);
}
