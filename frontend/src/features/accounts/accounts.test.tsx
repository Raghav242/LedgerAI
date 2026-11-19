import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ALICE, accountsPage, apiError, json, mockApi } from "@/test/mockApi";
import { renderWithProviders } from "@/test/render";
import { AccountsPanel } from "./AccountsPanel";
import { CreateAccountForm } from "./CreateAccountForm";

describe("CreateAccountForm", () => {
  it("requires a name before calling the API", async () => {
    const api = mockApi({});
    const { user } = renderWithProviders(<CreateAccountForm />);
    await user.click(await screen.findByRole("button", { name: "Create account" }));
    expect(await screen.findByText("Enter an account name")).toBeInTheDocument();
    expect(screen.getByLabelText("Account name")).toHaveAttribute("aria-invalid", "true");
    expect(api.requests).toHaveLength(0);
  });

  it("posts the backend's AccountCreate shape and reports the new account", async () => {
    const created = { ...ALICE, balance_minor: 0 };
    const api = mockApi({ "POST /api/v1/accounts": () => json(created, { status: 201 }) });
    const onCreated = vi.fn();
    const { user } = renderWithProviders(<CreateAccountForm onCreated={onCreated} />);

    await user.type(await screen.findByLabelText("Account name"), "  Alice Coffee  ");
    await user.click(screen.getByRole("button", { name: "Create account" }));

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(created));
    expect(api.calls("POST", "/api/v1/accounts")[0]?.body).toEqual({ name: "Alice Coffee", currency: "USD" });
  });

  it("offers only supported currencies", async () => {
    mockApi({});
    renderWithProviders(<CreateAccountForm />);
    const options = (await screen.findByLabelText("Currency")).querySelectorAll("option");
    expect([...options].map((o) => o.value)).toEqual(["USD"]);
  });

  it("shows server errors", async () => {
    mockApi({ "POST /api/v1/accounts": () => apiError(503, "RATE_LIMITER_UNAVAILABLE", "Rate limiter unavailable") });
    const { user } = renderWithProviders(<CreateAccountForm />);
    await user.type(await screen.findByLabelText("Account name"), "X");
    await user.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByText("Rate limiter unavailable")).toBeInTheDocument();
  });
});

describe("AccountsPanel", () => {
  it("shows a loading state, then the accounts", async () => {
    const api = mockApi({ "GET /api/v1/accounts": () => accountsPage() });
    renderWithProviders(<AccountsPanel selectedId={null} onSelect={() => {}} />);
    expect(screen.getByRole("status", { name: "Loading" })).toBeInTheDocument();
    expect(await screen.findByText("Alice Coffee")).toBeInTheDocument();
    expect(screen.getByText("$100.00")).toBeInTheDocument();
    // One bounded page, newest first, so a just-created account is always included.
    const query = api.calls("GET", "/api/v1/accounts")[0]!.query;
    expect([query.get("limit"), query.get("order")]).toEqual(["100", "desc"]);
  });

  it("shows an empty state when there are no accounts", async () => {
    mockApi({ "GET /api/v1/accounts": () => accountsPage([]) });
    renderWithProviders(<AccountsPanel selectedId={null} onSelect={() => {}} />);
    expect(await screen.findByText("No accounts yet")).toBeInTheDocument();
  });

  it("funds the selected account with integer cents", async () => {
    const api = mockApi({
      "GET /api/v1/accounts": () => accountsPage(),
      "POST /api/v1/accounts/:id/fund": () => json({ ...ALICE, balance_minor: 12_550 }),
    });
    const { user } = renderWithProviders(<AccountsPanel selectedId={ALICE.id} onSelect={() => {}} />);
    await user.type(await screen.findByLabelText("Add simulated funds (USD)"), "25.50");
    await user.click(screen.getByRole("button", { name: "Fund" }));

    expect(await screen.findByText("Added $25.50 of simulated funds.")).toBeInTheDocument();
    expect(api.calls("POST", `/api/v1/accounts/${ALICE.id}/fund`)[0]?.body).toEqual({ amount_minor: 2550 });
    // Balances are re-read from the server after funding.
    await waitFor(() => expect(api.calls("GET", "/api/v1/accounts").length).toBeGreaterThanOrEqual(2));
  });

  it("explains when the dev funding endpoint is disabled", async () => {
    mockApi({
      "GET /api/v1/accounts": () => accountsPage(),
      "POST /api/v1/accounts/:id/fund": () => apiError(404, "NOT_FOUND", "Not Found"),
    });
    const { user } = renderWithProviders(<AccountsPanel selectedId={ALICE.id} onSelect={() => {}} />);
    await user.click(await screen.findByRole("button", { name: "+$100.00" }));
    expect(await screen.findByText("Funding endpoint not enabled")).toBeInTheDocument();
  });
});
