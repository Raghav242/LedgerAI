import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { accountsPage, json, mockApi, readyOk } from "@/test/mockApi";
import { renderApp } from "@/test/render";

function stubBackend() {
  return mockApi({
    "GET /health/ready": readyOk,
    "GET /api/v1/accounts": () => accountsPage(),
    "GET /api/v1/transfers": () => json({ items: [], limit: 8, offset: 0 }),
    "GET /api/v1/reconciliation": () =>
      json({ ok: true, accounts_checked: 3, ledger_entries_checked: 4, ledger_total_minor: 0, balance_total_minor: 0, balance_mismatches: [], unbalanced_transfer_ids: [] }),
    "GET /api/v1/stats/summary": () =>
      json({
        merchant_account_count: 2,
        merchant_balance_total_minor: 10_500,
        ledger_entry_count: 4,
        transfers_by_kind: [
          { kind: "transfer", count: 0, volume_minor: 0 },
          { kind: "dev_funding", count: 2, volume_minor: 10_500 },
          { kind: "legacy_opening_balance", count: 0, volume_minor: 0 },
        ],
        daily_transfer_activity: [],
        generated_at: "2026-11-18T00:00:00Z",
      }),
    "GET /api/v1/ledger/entries": () => json({ items: [], limit: 20, offset: 0 }),
    "GET /api/v1/performance/benchmarks": () => json({ directory_found: true, runs: [] }),
  });
}

describe("application navigation", () => {
  it("renders the branded shell with every section", async () => {
    stubBackend();
    renderApp("/");
    const nav = screen.getByRole("navigation", { name: "Main" });
    for (const label of ["Overview", "Transaction Playground", "Transaction History", "Ledger & Reconciliation", "Experiment Lab", "System Topology", "AI Anomaly Lab", "Performance Lab"]) {
      expect(within(nav).getByRole("link", { name: new RegExp(label) })).toBeInTheDocument();
    }
    expect(screen.getAllByText("LedgerFlow AI").length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Simulated money only/i).length).toBeGreaterThan(0);
    expect(await screen.findByRole("heading", { name: "Overview" })).toBeInTheDocument();
  });

  it("marks no section as planned once every page is functional", () => {
    stubBackend();
    renderApp("/");
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).queryByText("Planned")).toBeNull();
    for (const label of ["Event Pipeline", "AI Anomaly Lab", "Performance Lab"]) {
      expect(within(nav).getByRole("link", { name: new RegExp(label) })).toBeInTheDocument();
    }
  });

  it("navigates between pages from the sidebar", async () => {
    stubBackend();
    const { user } = renderApp("/");
    const nav = screen.getByRole("navigation", { name: "Main" });

    await user.click(within(nav).getByRole("link", { name: /Transaction Playground/ }));
    expect(await screen.findByRole("heading", { name: "Transaction Playground" })).toBeInTheDocument();

    await user.click(within(nav).getByRole("link", { name: /Ledger & Reconciliation/ }));
    expect(await screen.findByRole("heading", { name: "Ledger & Reconciliation" })).toBeInTheDocument();

    await user.click(within(nav).getByRole("link", { name: /Performance Lab/ }));
    // First visit compiles the lazy chart chunk; allow for it under parallel test workers.
    expect(await screen.findByRole("heading", { name: "Performance Lab" }, { timeout: 15_000 })).toBeInTheDocument();
    // Recorded data is labelled as such; with no results, nothing is charted or invented.
    expect(screen.getByText("Recorded results, not live")).toBeInTheDocument();
    expect(await screen.findByText("No benchmark results recorded yet")).toBeInTheDocument();

    await user.click(within(nav).getByRole("link", { name: /System Topology/ }));
    expect(await screen.findByRole("heading", { name: "System Topology" })).toBeInTheDocument();
  });

  it("offers the same navigation from the menu button on small screens", async () => {
    stubBackend();
    const { user } = renderApp("/");
    await user.click(screen.getByRole("button", { name: "Open navigation" }));
    const sheet = await screen.findByRole("dialog");
    await user.click(within(sheet).getByRole("link", { name: /Experiment Lab/ }));
    expect(await screen.findByRole("heading", { name: "Experiment Lab" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("shows a not-found page for unknown routes", async () => {
    stubBackend();
    renderApp("/does-not-exist");
    expect(await screen.findByText("Page not found")).toBeInTheDocument();
  });

  it("shows API connectivity from /health/ready", async () => {
    stubBackend();
    renderApp("/topology");
    expect(await screen.findByRole("button", { name: "API connected" })).toBeInTheDocument();
  });
});
