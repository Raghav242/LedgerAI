import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { accountsPage, apiError, json, mockApi, readyOk, transferBody } from "@/test/mockApi";
import { renderApp } from "@/test/render";

const summary = {
  merchant_account_count: 67,
  merchant_balance_total_minor: 6_436_050,
  ledger_entry_count: 398,
  transfers_by_kind: [
    { kind: "transfer", count: 166, volume_minor: 90_984 },
    { kind: "dev_funding", count: 31, volume_minor: 6_425_000 },
    { kind: "legacy_opening_balance", count: 2, volume_minor: 11_050 },
  ],
  daily_transfer_activity: [
    { date: "2026-11-17", count: 0, volume_minor: 0 },
    { date: "2026-11-18", count: 3, volume_minor: 7_500 },
  ],
  generated_at: "2026-11-18T12:00:00Z",
};
const reconciliation = { ok: true, accounts_checked: 68, ledger_entries_checked: 398, ledger_total_minor: 0, balance_total_minor: 0, balance_mismatches: [], unbalanced_transfer_ids: [] };

describe("Overview", () => {
  it("shows real aggregates from the summary endpoint", async () => {
    mockApi({
      "GET /health/ready": readyOk,
      "GET /api/v1/accounts": () => accountsPage(),
      "GET /api/v1/stats/summary": () => json(summary),
      "GET /api/v1/transfers": () => json({ items: [transferBody()], limit: 8, offset: 0 }),
      "GET /api/v1/reconciliation": () => json(reconciliation),
    });
    renderApp("/");
    expect(await screen.findByText("67")).toBeInTheDocument();
    expect(screen.getByText("166")).toBeInTheDocument();
    expect(screen.getByText("$909.84")).toBeInTheDocument();
    expect(screen.getByText("$64,360.50")).toBeInTheDocument();
    expect(await screen.findByText("Passed: every balance matches the ledger")).toBeInTheDocument();
    expect(await screen.findByText("Ready: PostgreSQL and Redis reachable")).toBeInTheDocument();
    expect(await screen.findByText("Alice Coffee")).toBeInTheDocument(); // recent transfer source name
    expect(screen.getByText(/3 merchant transfers/)).toBeInTheDocument();
  });

  it("shows empty states when there is no activity", async () => {
    mockApi({
      "GET /health/ready": readyOk,
      "GET /api/v1/accounts": () => accountsPage([]),
      "GET /api/v1/stats/summary": () =>
        json({ ...summary, merchant_account_count: 0, transfers_by_kind: summary.transfers_by_kind.map((k) => ({ ...k, count: 0, volume_minor: 0 })), daily_transfer_activity: [{ date: "2026-11-18", count: 0, volume_minor: 0 }] }),
      "GET /api/v1/transfers": () => json({ items: [], limit: 8, offset: 0 }),
      "GET /api/v1/reconciliation": () => json(reconciliation),
    });
    renderApp("/");
    expect(await screen.findByText("No transactions yet")).toBeInTheDocument();
    expect(await screen.findByText("No transfers in this window")).toBeInTheDocument();
  });

  it("shows errors instead of numbers when the API is down", async () => {
    mockApi({
      "GET /health/ready": () => {
        throw new TypeError("Failed to fetch");
      },
      "GET /api/v1/accounts": () => apiError(502, "HTTP_502", "Bad gateway"),
      "GET /api/v1/stats/summary": () => apiError(502, "HTTP_502", "Bad gateway"),
      "GET /api/v1/transfers": () => apiError(502, "HTTP_502", "Bad gateway"),
      "GET /api/v1/reconciliation": () => apiError(502, "HTTP_502", "Bad gateway"),
    });
    renderApp("/");
    expect((await screen.findAllByText("Unavailable")).length).toBe(4);
    expect(await screen.findByRole("button", { name: "API unreachable" })).toBeInTheDocument();
  });
});
