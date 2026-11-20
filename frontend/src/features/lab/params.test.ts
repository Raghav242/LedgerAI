import { describe, expect, it } from "vitest";
import { CATALOG } from "@/test/labFixtures";
import { defaultRawParams, formatParam, validateParams } from "./params";

const concurrency = CATALOG.find((e) => e.id === "concurrency")!.params;

describe("experiment parameter validation", () => {
  it("shows money defaults in dollars and sends integer cents", () => {
    const raw = defaultRawParams(concurrency);
    expect(raw).toEqual({ requests: "100", amount_minor: "10.00", funding_minor: "100.00" });
    expect(validateParams(concurrency, raw)).toEqual({
      ok: true,
      params: { requests: 100, amount_minor: 1000, funding_minor: 10000 },
    });
  });

  it("converts dollar input exactly (no floating point)", () => {
    const result = validateParams(concurrency, { requests: "3", amount_minor: "$0.29", funding_minor: "1,000.10" });
    expect(result).toEqual({ ok: true, params: { requests: 3, amount_minor: 29, funding_minor: 100010 } });
  });

  it.each([
    ["requests", "", "Enter a value"],
    ["requests", "abc", "Must be a whole number"],
    ["requests", "10.5", "Must be a whole number"],
    ["requests", "201", "Must be between 2 and 200"],
    ["requests", "1", "Must be between 2 and 200"],
    ["requests", "-5", "Must be between 2 and 200"],
    ["amount_minor", "1.999", "Enter a dollar amount like 10.00"],
    ["amount_minor", "abc", "Enter a dollar amount like 10.00"],
    ["amount_minor", "1000.01", "Must be between $0.01 and $1,000.00"],
    ["funding_minor", "0", "Enter a dollar amount like 10.00"],
  ])("rejects %s=%j", (name, value, message) => {
    const result = validateParams(concurrency, { ...defaultRawParams(concurrency), [name]: value });
    expect(result).toEqual({ ok: false, errors: { [name]: message } });
  });

  it("formats bounds in the parameter's unit", () => {
    expect(formatParam({ unit: "cents" }, 100_000)).toBe("$1,000.00");
    expect(formatParam({ unit: "seconds" }, 5)).toBe("5 s");
    expect(formatParam({ unit: "requests" }, 200)).toBe("200");
  });
});
