import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ALICE, BOB, accountsPage, apiError, json, mockApi, readyOk, transferBody } from "@/test/mockApi";
import { renderApp } from "@/test/render";

type Routes = Parameters<typeof mockApi>[0];

function setup(transferHandlers: Routes[string]) {
  const api = mockApi({
    "GET /health/ready": readyOk,
    "GET /api/v1/accounts": () => accountsPage(),
    "POST /api/v1/transfers": transferHandlers,
  });
  const view = renderApp("/playground");
  return { api, ...view };
}

async function fillTransfer(user: ReturnType<typeof setup>["user"], amount: string, source = ALICE.id, destination = BOB.id) {
  const form = await screen.findByRole("form", { name: "Transfer funds" });
  await user.selectOptions(within(form).getByLabelText("Source account"), source);
  await user.selectOptions(within(form).getByLabelText("Destination account"), destination);
  const amountInput = within(form).getByLabelText("Amount (USD)");
  await user.clear(amountInput);
  await user.type(amountInput, amount);
  return form;
}

function send(user: ReturnType<typeof setup>["user"]) {
  return user.click(screen.getByRole("button", { name: "Send transfer" }));
}

function outcome() {
  return screen.findByTestId("transfer-outcome");
}

describe("transfer validation", () => {
  it("requires accounts and a valid amount before sending anything", async () => {
    const { api, user } = setup(() => json(transferBody(), { status: 201 }));
    await screen.findByRole("form", { name: "Transfer funds" });
    await send(user);
    expect(await screen.findByText("Select a source account")).toBeInTheDocument();
    expect(screen.getByText("Select a destination account")).toBeInTheDocument();
    expect(screen.getByText("Enter an amount")).toBeInTheDocument();
    expect(api.calls("POST", "/api/v1/transfers")).toHaveLength(0);
  });

  it("rejects the same source and destination", async () => {
    const { api, user } = setup(() => json(transferBody(), { status: 201 }));
    await fillTransfer(user, "5", ALICE.id, ALICE.id);
    await send(user);
    expect(await screen.findByText("Source and destination must be different accounts")).toBeInTheDocument();
    expect(api.calls("POST", "/api/v1/transfers")).toHaveLength(0);
  });

  it.each([
    ["0", "greater than $0.00"],
    ["-3", "positive"],
    ["1.999", "two decimal places"],
  ])("rejects amount %j", async (amount, message) => {
    const { api, user } = setup(() => json(transferBody(), { status: 201 }));
    await fillTransfer(user, amount);
    await send(user);
    expect(await screen.findByText((text) => text.includes(message))).toBeInTheDocument();
    expect(api.calls("POST", "/api/v1/transfers")).toHaveLength(0);
  });

  it("shows the exact minor units that will be sent", async () => {
    const { user } = setup(() => json(transferBody(), { status: 201 }));
    await fillTransfer(user, "0.29");
    expect(screen.getByText("amount_minor: 29")).toBeInTheDocument();
  });
});

describe("transfer results", () => {
  it("sends integer cents with an Idempotency-Key and shows the success", async () => {
    const { api, user } = setup(() => json(transferBody(), { status: 201, headers: { "x-served-by": "api-2", "x-request-id": "req-9" } }));
    await fillTransfer(user, "25.50");
    const key = screen.getByTestId("idempotency-key").textContent!;
    await send(user);

    const result = await outcome();
    expect(result).toHaveAttribute("data-category", "succeeded");
    expect(within(result).getByText("Transfer completed")).toBeInTheDocument();
    expect(within(result).getByText("$25.50")).toBeInTheDocument();
    // Response metadata is secondary: behind "Request details", summarised in its label.
    const detailsToggle = within(result).getByRole("button", { name: /Request details/ });
    expect(detailsToggle).toHaveTextContent("HTTP 201 · api-2");
    expect(within(result).queryByText("req-9")).toBeNull();
    await user.click(detailsToggle);
    const details = within(result).getByTestId("request-details");
    expect(within(details).getByText("201")).toBeInTheDocument();
    expect(within(details).getByText("api-2")).toBeInTheDocument();
    expect(within(details).getByText("req-9")).toBeInTheDocument();
    expect(within(details).getByText("33333333…3333")).toBeInTheDocument(); // transaction id
    expect(within(details).getByText(key)).toBeInTheDocument();

    const [post] = api.calls("POST", "/api/v1/transfers");
    expect(post?.body).toEqual({ source_account_id: ALICE.id, destination_account_id: BOB.id, amount_minor: 2550, currency: "USD" });
    expect(post?.headers["idempotency-key"]).toBe(key);
    // Balances refresh only after confirmed success.
    await waitFor(() => expect(api.calls("GET", "/api/v1/accounts").length).toBeGreaterThanOrEqual(2));
  });

  it("shows insufficient funds as a final rejection", async () => {
    const { api, user } = setup(() =>
      apiError(409, "INSUFFICIENT_FUNDS", "Source account has insufficient funds", { balance_minor: 10_000, requested_minor: 999_900 }),
    );
    await fillTransfer(user, "9999");
    await send(user);
    const result = await outcome();
    expect(result).toHaveAttribute("data-category", "rejected");
    expect(within(result).getByText("Insufficient funds (409)")).toBeInTheDocument();
    expect(within(result).getByText(/final, stored outcome/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Retry with same key/ })).toBeNull();
    const accountReads = api.calls("GET", "/api/v1/accounts").length;
    await new Promise((r) => setTimeout(r, 20));
    expect(api.calls("GET", "/api/v1/accounts").length).toBe(accountReads); // no refresh on rejection
  });

  it("handles 429 with Retry-After: explains the wait and blocks early retries", async () => {
    const { user } = setup(() => apiError(429, "RATE_LIMITED", "Rate limit exceeded; retry later", { retry_after_seconds: 3 }, { "retry-after": "3" }));
    await fillTransfer(user, "1");
    await send(user);
    const result = await outcome();
    expect(result).toHaveAttribute("data-category", "retryable");
    expect(within(result).getByText("Rate limited (429)")).toBeInTheDocument();
    expect(within(result).getByText(/Retry-After: 3 s/)).toBeInTheDocument();
    const retry = screen.getByRole("button", { name: /Retry with same key in \d s/ });
    expect(retry).toBeDisabled();
    // The form is locked to the key while the outcome is not final.
    expect(screen.getByLabelText("Amount (USD)")).toBeDisabled();
  });

  it("shows 503 rate-limiter outage (fail closed)", async () => {
    const { user } = setup(() => apiError(503, "RATE_LIMITER_UNAVAILABLE", "Rate limiter unavailable; write rejected"));
    await fillTransfer(user, "1");
    await send(user);
    const result = await outcome();
    expect(within(result).getByText("Rate limiter unavailable (503)")).toBeInTheDocument();
    expect(within(result).getByText(/fail closed/)).toBeInTheDocument();
  });

  it("preserves the idempotency key when retrying an ambiguous failure", async () => {
    const { api, user } = setup([
      () => {
        throw new TypeError("Failed to fetch");
      },
      () => json(transferBody(), { status: 201, headers: { "idempotent-replayed": "true" } }),
    ]);
    await fillTransfer(user, "25.50");
    await send(user);

    const first = await outcome();
    expect(first).toHaveAttribute("data-category", "ambiguous");
    expect(within(first).getByText(/Retry with the SAME idempotency key/)).toBeInTheDocument();
    expect(screen.getByText(/could move the money twice/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Retry with same key" }));
    await waitFor(async () => expect(await outcome()).toHaveAttribute("data-category", "succeeded"));
    expect(screen.getByText("Replayed original success")).toBeInTheDocument();

    const posts = api.calls("POST", "/api/v1/transfers");
    expect(posts).toHaveLength(2);
    expect(posts[1]?.headers["idempotency-key"]).toBe(posts[0]?.headers["idempotency-key"]);
    expect(posts[1]?.body).toEqual(posts[0]?.body);
  });

  it("uses a new key for a new logical transfer after a final outcome", async () => {
    const { api, user } = setup(() => json(transferBody(), { status: 201 }));
    await fillTransfer(user, "1");
    await send(user);
    await outcome();
    await send(user); // a second, separate transfer
    await waitFor(() => expect(api.calls("POST", "/api/v1/transfers")).toHaveLength(2));
    const [a, b] = api.calls("POST", "/api/v1/transfers");
    expect(a?.headers["idempotency-key"]).not.toBe(b?.headers["idempotency-key"]);
  });

  it("replays a finished transfer with the same key on request", async () => {
    const { api, user } = setup([
      () => json(transferBody(), { status: 201 }),
      () => json(transferBody(), { status: 201, headers: { "idempotent-replayed": "true" } }),
    ]);
    await fillTransfer(user, "1");
    await send(user);
    await outcome();
    await user.click(screen.getByRole("button", { name: /Send again with same key/ }));
    expect(await screen.findByText("Replayed original success")).toBeInTheDocument();
    const [a, b] = api.calls("POST", "/api/v1/transfers");
    expect(b?.headers["idempotency-key"]).toBe(a?.headers["idempotency-key"]);
  });
});

describe("request path (flow visualizer)", () => {
  function flow() {
    return screen.getByRole("figure", { name: "Request flow" });
  }

  async function openPath(user: ReturnType<typeof setup>["user"]) {
    await user.click(await screen.findByRole("button", { name: /Request path/ }));
  }

  it("is not shown before anything is sent", async () => {
    setup(() => json(transferBody(), { status: 201 }));
    await screen.findByRole("form", { name: "Transfer funds" });
    expect(screen.queryByRole("button", { name: /Request path/ })).toBeNull();
    expect(screen.queryByRole("figure", { name: "Request flow" })).toBeNull();
  });

  it("is collapsed after an attempt, summarised in one line, and shows the serving replica and token count", async () => {
    const { user } = setup(() =>
      json(transferBody(), {
        status: 201,
        headers: { "x-served-by": "api-2", "x-ratelimit-policy": "write", "x-ratelimit-limit": "20", "x-ratelimit-remaining": "17" },
      }),
    );
    await fillTransfer(user, "25.50");
    await send(user);
    await outcome();
    const toggle = await screen.findByRole("button", { name: /Request path/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveTextContent("Committed (201) · api-2");
    await openPath(user);
    await waitFor(() => expect(flow().querySelector('[data-node="database"]')).toHaveAttribute("data-state", "success"));
    expect(flow().querySelector('[data-node="replica"]')).toHaveTextContent("api-2");
    await user.click(within(flow()).getByRole("button", { name: /^Redis:/ }));
    const details = screen.getByRole("region", { name: "Redis details" });
    expect(within(details).getByText("17/20 tokens left")).toBeInTheDocument();
    expect(within(details).getByText("Observed")).toBeInTheDocument(); // X-RateLimit-* headers were present
  });

  it("shows a replay stopping at the idempotency claim", async () => {
    const { user } = setup(() => json(transferBody(), { status: 201, headers: { "idempotent-replayed": "true", "x-served-by": "api-1" } }));
    await fillTransfer(user, "25.50");
    await send(user);
    await outcome();
    await openPath(user);
    await waitFor(() => expect(flow().querySelector('[data-node="database"]')).toHaveAttribute("data-state", "replayed"));
    await user.click(within(flow()).getByRole("button", { name: /^PostgreSQL:/ }));
    const stages = screen.getByRole("list", { name: "PostgreSQL stages" });
    expect(stages.querySelector('[data-stage="idempotency"]')).toHaveAttribute("data-state", "replayed");
    expect(stages.querySelector('[data-stage="postgres"]')).toHaveAttribute("data-state", "skipped");
  });

  it("shows a 429 blocked at Redis", async () => {
    const { user } = setup(() => apiError(429, "RATE_LIMITED", "Rate limit exceeded; retry later", { policy: "write" }, { "retry-after": "1", "x-served-by": "api-1" }));
    await fillTransfer(user, "25.50");
    await send(user);
    await outcome();
    await openPath(user);
    await waitFor(() => expect(flow().querySelector('[data-node="redis"]')).toHaveAttribute("data-state", "blocked"));
    expect(flow().querySelector('[data-node="database"]')).toHaveAttribute("data-state", "skipped");
    expect(flow().querySelector('[data-node="response"]')).toHaveTextContent("429");
  });
});

describe("result summary", () => {
  it("shows confirmed balances from the refreshed account list after a new transfer", async () => {
    const after = [
      { ...ALICE, balance_minor: 7_450 },
      { ...BOB, balance_minor: 3_050 },
    ];
    mockApi({
      "GET /health/ready": readyOk,
      "GET /api/v1/accounts": [() => accountsPage(), () => accountsPage(after)],
      "POST /api/v1/transfers": () => json(transferBody(), { status: 201 }),
    });
    const { user } = renderApp("/playground");
    await fillTransfer(user, "25.50");
    await send(user);
    const balances = await screen.findByTestId("balances-now");
    await waitFor(() => expect(balances).toHaveTextContent("Alice Coffee $74.50"));
    expect(balances).toHaveTextContent("Bob Bakery $30.50");
  });

  it("never shows balances for an uncertain outcome, and keeps the attempt log reachable", async () => {
    const { user } = setup(() => {
      throw new TypeError("Failed to fetch");
    });
    await fillTransfer(user, "25.50");
    await send(user);
    const result = await outcome();
    expect(result).toHaveAttribute("data-category", "ambiguous");
    expect(screen.queryByTestId("balances-now")).toBeNull();
    const attempts = screen.getByRole("button", { name: /Attempts for this logical transfer/ });
    expect(attempts).toHaveAttribute("aria-expanded", "false");
    await user.click(attempts);
    expect(screen.getByText(/#1 Submit · no response NETWORK_ERROR/)).toBeInTheDocument();
  });
});
