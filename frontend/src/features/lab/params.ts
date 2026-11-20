/**
 * Client-side validation of experiment parameters, built from the backend's ParamSpecs
 * (derived from the same Pydantic models that enforce the bounds). The backend re-validates
 * everything; this only gives immediate feedback.
 *
 * Money parameters (unit "cents") are edited in dollars ("10.00") and converted to integer
 * cents exactly (parseUsdToMinor, no floating point) before they are sent.
 */
import { formatMinor, parseUsdToMinor } from "@/lib/money";
import type { ParamSpec } from "@/types/api";

export type RawParams = Record<string, string>;

export type ParamsValidation =
  | { ok: true; params: Record<string, number> }
  | { ok: false; errors: Record<string, string> };

function centsToDollarText(cents: number): string {
  return (cents / 100).toFixed(2);
}

export function defaultRawParams(specs: ParamSpec[]): RawParams {
  return Object.fromEntries(specs.map((s) => [s.name, s.unit === "cents" ? centsToDollarText(s.default) : String(s.default)]));
}

export function validateParams(specs: ParamSpec[], raw: RawParams): ParamsValidation {
  const params: Record<string, number> = {};
  const errors: Record<string, string> = {};
  for (const spec of specs) {
    const text = (raw[spec.name] ?? "").trim();
    if (text === "") {
      errors[spec.name] = "Enter a value";
      continue;
    }
    let value: number;
    if (spec.unit === "cents") {
      const parsed = parseUsdToMinor(text.replace(/^\$/, ""));
      if (!parsed.ok) {
        errors[spec.name] = "Enter a dollar amount like 10.00";
        continue;
      }
      value = parsed.minor;
    } else {
      if (!/^-?\d+$/.test(text)) {
        errors[spec.name] = "Must be a whole number";
        continue;
      }
      value = Number(text);
    }
    if (value < spec.minimum || value > spec.maximum) {
      errors[spec.name] = `Must be between ${formatParam(spec, spec.minimum)} and ${formatParam(spec, spec.maximum)}`;
      continue;
    }
    params[spec.name] = value;
  }
  return Object.keys(errors).length ? { ok: false, errors } : { ok: true, params };
}

export function formatParam(spec: Pick<ParamSpec, "unit">, value: number): string {
  if (spec.unit === "cents") return formatMinor(value);
  if (spec.unit === "seconds") return `${value} s`;
  return String(value);
}

/** Short field labels; the backend's longer titles stay in "How this works". */
export const SHORT_LABEL: Record<string, string> = {
  requests: "Requests",
  amount_minor: "Amount",
  funding_minor: "Starting balance",
  duplicates: "Duplicates",
  burst_requests: "Burst size",
  refill_wait_seconds: "Refill wait",
  probe_requests: "Follow-up requests",
  concurrency: "In flight",
};
