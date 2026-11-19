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
    expect(within(result).getByText("201")).toBeInTheDocument();
    expect(within(result).getByText("api-2")).toBeInTheDocument();
    expect(within(result).getByText("33333333…3333")).toBeInTheDocument(); // transaction id
    expect(within(result).getByText("$25.50")).toBeInTheDocument();

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
