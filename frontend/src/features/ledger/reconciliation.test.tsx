import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { apiError, json, mockApi } from "@/test/mockApi";
import { renderWithProviders } from "@/test/render";
import { ReconciliationPanel } from "./ReconciliationPanel";

const passing = {
  ok: true,
  accounts_checked: 68,
  ledger_entries_checked: 398,
  ledger_total_minor: 0,
  balance_total_minor: 0,
  balance_mismatches: [],
  unbalanced_transfer_ids: [],
};

describe("ReconciliationPanel", () => {
  it("shows loading, then a pass only when the backend says ok", async () => {
    mockApi({ "GET /api/v1/reconciliation": () => json(passing) });
    const { user } = renderWithProviders(<ReconciliationPanel />);
    expect(screen.getByRole("status", { name: "Loading" })).toBeInTheDocument();
    const verdict = await screen.findByTestId("reconciliation-verdict");
    expect(verdict).toHaveAttribute("data-ok", "true");
    expect(within(verdict).getByText("Ledger balanced — all checks passed")).toBeInTheDocument();
    expect(within(verdict).getByText(/68 accounts · 398 ledger entries/)).toBeInTheDocument();
    // Passing invariants are one click away, not on screen by default.
    expect(screen.queryByRole("list", { name: "Invariant checks" })).toBeNull();
    const toggle = screen.getByRole("button", { name: /Invariants/ });
    expect(toggle).toHaveTextContent("4 of 4 hold");
    await user.click(toggle);
    const checks = screen.getByRole("list", { name: "Invariant checks" });
    expect(within(checks).getAllByText("Holds")).toHaveLength(4);
    // Each check expands to its observed value.
    await user.click(within(checks).getByRole("button", { name: /All ledger entries sum to zero/ }));
    expect(within(checks).getByText(/over 398 entries/)).toBeInTheDocument();
  });

  it("shows failures and the reported discrepancies", async () => {
    mockApi({
      "GET /api/v1/reconciliation": () =>
        json({
          ...passing,
          ok: false,
          balance_total_minor: 500,
          balance_mismatches: [{ account_id: "44444444-4444-4444-8444-444444444444", balance_minor: 1500, ledger_balance_minor: 1000 }],
          unbalanced_transfer_ids: ["55555555-5555-4555-8555-555555555555"],
        }),
    });
    renderWithProviders(<ReconciliationPanel />);
    const verdict = await screen.findByTestId("reconciliation-verdict");
    expect(verdict).toHaveAttribute("data-ok", "false");
    expect(within(verdict).getByText("Reconciliation failed — discrepancies detected")).toBeInTheDocument();
    // On failure the invariants and discrepancies open by themselves.
    expect(within(screen.getByRole("list", { name: "Invariant checks" })).getAllByText("Violated")).toHaveLength(3);
    expect(screen.getByText("Balance mismatches")).toBeInTheDocument();
    expect(screen.getByText("+$5.00")).toBeInTheDocument(); // difference 1500 - 1000
    expect(screen.getByText("Unbalanced transfers")).toBeInTheDocument();
  });

  it("never claims a pass when the request fails", async () => {
    mockApi({ "GET /api/v1/reconciliation": () => apiError(500, "INTERNAL_ERROR", "An unexpected error occurred") });
    renderWithProviders(<ReconciliationPanel />);
    expect(await screen.findByText("Request failed (500)")).toBeInTheDocument();
    expect(screen.queryByText(/Ledger balanced/)).toBeNull();
    expect(screen.getByRole("button", { name: /Try again/ })).toBeInTheDocument();
  });

  it("re-runs on demand", async () => {
    const api = mockApi({ "GET /api/v1/reconciliation": () => json(passing) });
    const { user } = renderWithProviders(<ReconciliationPanel />);
    await screen.findByTestId("reconciliation-verdict");
    await user.click(screen.getByRole("button", { name: /Run again/ }));
    await screen.findByTestId("reconciliation-verdict");
    expect(api.calls("GET", "/api/v1/reconciliation").length).toBe(2);
  });
});
