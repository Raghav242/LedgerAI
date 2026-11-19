import { describe, expect, it } from "vitest";
import { formatMinor, MAX_AMOUNT_MINOR, parseUsdToMinor } from "./money";

describe("parseUsdToMinor", () => {
  it.each([
    ["25", 2500],
    ["25.5", 2550],
    ["25.50", 2550],
    ["0.01", 1],
    ["19.99", 1999],
    ["1,250.75", 125075],
    ["$12.34", 1234],
    ["  7.10 ", 710],
    ["0.29", 29], // 0.29 * 100 = 28.999999999999996 in floating point
    ["1.15", 115], // 1.15 * 100 = 114.99999999999999 in floating point
    ["1000000000", MAX_AMOUNT_MINOR],
  ])("parses %s as %d cents exactly", (input, expected) => {
    expect(parseUsdToMinor(input)).toEqual({ ok: true, minor: expected });
  });

  it.each([
    ["", "Enter an amount"],
    ["0", "greater than $0.00"],
    ["0.00", "greater than $0.00"],
    ["-5", "positive"],
    ["10.505", "two decimal places"],
    ["abc", "valid amount"],
    ["1e3", "valid amount"],
    ["1,00", "valid amount"],
    ["1000000000.01", "limit"],
    ["99999999999999999", "limit"],
  ])("rejects %j", (input, message) => {
    const result = parseUsdToMinor(input);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain(message);
  });

  it("never produces a non-integer", () => {
    for (let cents = 1; cents < 1000; cents += 7) {
      const text = `${Math.floor(cents / 100)}.${String(cents % 100).padStart(2, "0")}`;
      const result = parseUsdToMinor(text);
      expect(result).toEqual({ ok: true, minor: cents });
    }
  });
});

describe("formatMinor", () => {
  it.each([
    [0, "$0.00"],
    [1, "$0.01"],
    [2550, "$25.50"],
    [123456789, "$1,234,567.89"],
    [-2500, "-$25.00"],
  ])("formats %d as %s", (minor, expected) => {
    expect(formatMinor(minor)).toBe(expected);
  });

  it("adds an explicit sign when asked", () => {
    expect(formatMinor(2500, { signed: true })).toBe("+$25.00");
    expect(formatMinor(-2500, { signed: true })).toBe("-$25.00");
    expect(formatMinor(0, { signed: true })).toBe("$0.00");
  });

  it("refuses non-integer input instead of rounding silently", () => {
    expect(() => formatMinor(10.5)).toThrow(RangeError);
  });

  it("round-trips with the parser", () => {
    for (const minor of [1, 99, 100, 2550, 1_000_000, MAX_AMOUNT_MINOR]) {
      expect(parseUsdToMinor(formatMinor(minor))).toEqual({ ok: true, minor });
    }
  });
});
