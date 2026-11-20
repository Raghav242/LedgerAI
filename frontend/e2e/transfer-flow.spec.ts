import { expect, test, type ConsoleMessage, type Page } from "@playwright/test";
import path from "node:path";

/**
 * Full flow against the real backend: create two accounts, fund one, transfer, verify
 * balances/history/ledger/reconciliation, and an insufficient-funds rejection.
 * Screenshots go to ../docs/screenshots when SCREENSHOTS=1.
 */
const SHOTS = process.env.SCREENSHOTS === "1";
const shotDir = path.resolve(import.meta.dirname, "../../docs/screenshots");

/** Full-height capture by growing the viewport (fullPage would repeat sticky headers). */
async function shot(page: Page, name: string) {
  if (!SHOTS) return;
  const original = page.viewportSize()!;
  const height = await page.evaluate(() => document.documentElement.scrollHeight);
  await page.setViewportSize({ width: original.width, height: Math.min(height, 4000) });
  await page.waitForTimeout(150);
  await page.screenshot({ path: path.join(shotDir, `${name}.png`) });
  await page.setViewportSize(original);
}

function trackConsole(page: Page) {
  const problems: string[] = [];
  page.on("console", (msg: ConsoleMessage) => {
    if (msg.type() !== "error" && msg.type() !== "warning") return;
    // The browser itself logs non-2xx responses. The flow deliberately triggers a 409
    // (insufficient funds); that network log line is expected, anything else is a problem.
    if (/Failed to load resource: .*status of 409/.test(msg.text())) return;
    problems.push(`${msg.type()}: ${msg.text()}`);
  });
  page.on("pageerror", (err) => problems.push(`pageerror: ${err.message}`));
  return problems;
}

async function createAccount(page: Page, name: string) {
  await page.getByRole("button", { name: "New account" }).click();
  const dialog = page.getByRole("dialog", { name: "Create a virtual account" });
  await dialog.getByLabel("Account name").fill(name);
  await dialog.getByRole("button", { name: "Create account" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByRole("list", { name: "Accounts" }).getByRole("button", { name: new RegExp(name) })).toBeVisible();
}

function accountRow(page: Page, name: string) {
  return page.getByRole("list", { name: "Accounts" }).getByRole("button", { name: new RegExp(name) });
}

test("create, fund, transfer, and verify through the real API", async ({ page }) => {
  const problems = trackConsole(page);
  const run = Date.now().toString(36);
  const alice = `E2E Alice ${run}`;
  const bob = `E2E Bob ${run}`;

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  await expect(page.getByRole("button", { name: "API connected" })).toBeVisible();
  const systemStatus = page.getByRole("region", { name: "System status" });
  await expect(systemStatus.getByText("Ledger balanced")).toBeVisible();
  await expect(systemStatus.getByText("API ready")).toBeVisible();

  // --- accounts -------------------------------------------------------------------------
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: /Transaction Playground/ }).click();
  await expect(page.getByRole("heading", { name: "Transaction Playground" })).toBeVisible();
  await createAccount(page, alice);
  await createAccount(page, bob);

  // --- fund Alice with $100.00 of simulated money ------------------------------------------
  await accountRow(page, alice).click();
  const selected = page.getByRole("region", { name: "Selected account" });
  await selected.getByLabel("Add simulated funds (USD)").fill("100");
  await selected.getByRole("button", { name: "Fund", exact: true }).click();
  await expect(selected.getByText("Added $100.00 of simulated funds.")).toBeVisible();
  await expect(accountRow(page, alice)).toContainText("$100.00");

  // --- transfer $25.50 Alice -> Bob ----------------------------------------------------------
  const form = page.getByRole("form", { name: "Transfer funds" });
  await form.getByLabel("Source account").selectOption({ label: `${alice} · $100.00` });
  await form.getByLabel("Destination account").selectOption({ label: bob });
  await form.getByLabel("Amount (USD)").fill("25.50");
  await expect(form.getByText("amount_minor: 2550")).toBeVisible();
  const key = (await page.getByTestId("idempotency-key").textContent())!.trim();
  await form.getByRole("button", { name: "Send transfer" }).click();

  const outcome = page.getByTestId("transfer-outcome");
  await expect(outcome).toHaveAttribute("data-category", "succeeded");
  await expect(outcome.getByText("Transfer completed")).toBeVisible();
  await expect(accountRow(page, alice)).toContainText("$74.50");
  await expect(accountRow(page, bob)).toContainText("$25.50");
  await expect(outcome.getByTestId("balances-now")).toContainText("$74.50");
  // Response metadata is one click away.
  await outcome.getByRole("button", { name: /Request details/ }).click();
  await expect(outcome.getByText(key)).toBeVisible();
  await expect(outcome.getByTestId("request-details").getByText(/^api-\d$/)).toBeVisible(); // X-Served-By from a real replica
  // The request path (collapsed card): through a real replica, all the way to PostgreSQL.
  await page.getByRole("button", { name: /Request path/ }).click();
  const flow = page.getByRole("figure", { name: "Request flow" });
  await expect(flow.locator('[data-node="database"]')).toHaveAttribute("data-state", "success");
  await expect(flow.locator('[data-node="replica"]')).toContainText(/api-\d/);
  await expect(page.getByTestId("flow-evidence")).toContainText("Observed: Client, NGINX, API replica, Redis, PostgreSQL, Response");

  // Replaying the same key moves no money.
  await page.getByRole("button", { name: /Send again with same key/ }).click();
  await expect(outcome.getByText("Replayed original success")).toBeVisible();
  await expect(outcome.getByText("Idempotent-Replayed", { exact: true })).toBeVisible();
  await expect(flow.locator('[data-node="database"]')).toHaveAttribute("data-state", "replayed");
  await expect(accountRow(page, alice)).toContainText("$74.50");
  await shot(page, "playground-success");

  // --- insufficient funds: a new logical transfer gets a new key ------------------------------
  await page.getByRole("button", { name: "New transfer (new key)" }).click();
  const secondKey = (await page.getByTestId("idempotency-key").textContent())!.trim();
  expect(secondKey).not.toBe(key);
  await form.getByLabel("Source account").selectOption({ label: `${alice} · $74.50` });
  await form.getByLabel("Destination account").selectOption({ label: bob });
  await form.getByLabel("Amount (USD)").fill("1000");
  await form.getByRole("button", { name: "Send transfer" }).click();
  await expect(outcome).toHaveAttribute("data-category", "rejected");
  await expect(outcome.getByText("Insufficient funds (409)")).toBeVisible();
  await expect(flow.locator('[data-node="database"]')).toHaveAttribute("data-state", "blocked");
  await flow.locator('[data-node="database"]').click();
  await expect(page.getByRole("region", { name: "PostgreSQL details" }).locator('[data-stage="postgres"]')).toHaveAttribute("data-state", "blocked");
  await expect(accountRow(page, alice)).toContainText("$74.50");
  await shot(page, "playground-insufficient-funds");

  // --- history and ledger entries --------------------------------------------------------------
  await page.getByRole("button", { name: "New transfer (new key)" }).click();
  await page.goto("/history");
  await expect(page.getByRole("heading", { name: "Transaction History" })).toBeVisible();
  const firstRow = page.getByRole("row", { name: /Open transfer/ }).first();
  await expect(firstRow).toContainText(alice);
  await expect(firstRow).toContainText(bob);
  await expect(firstRow).toContainText("$25.50");
  await firstRow.click();
  const sheet = page.getByRole("dialog", { name: "Transfer details" });
  await expect(sheet.getByText("Balanced: exactly one debit and one credit that sum to zero.")).toBeVisible();
  await expect(sheet.getByText("-$25.50")).toBeVisible();
  await expect(sheet.getByText("+$25.50")).toBeVisible();
  await shot(page, "history-detail");
  await page.keyboard.press("Escape");

  // --- reconciliation ------------------------------------------------------------------------
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: /Ledger & Reconciliation/ }).click();
  await expect(page.getByTestId("reconciliation-verdict")).toHaveAttribute("data-ok", "true");
  await expect(page.getByText("Ledger balanced — all checks passed")).toBeVisible();
  await page.getByRole("button", { name: /Invariants/ }).click();
  await expect(page.getByRole("list", { name: "Invariant checks" }).getByText("Holds")).toHaveCount(4);
  await page.getByRole("button", { name: /How double-entry works/ }).click();
  await expect(page.getByText("Illustrative example")).toBeVisible();
  await shot(page, "ledger-reconciliation");

  // Roadmap pages stay clearly labelled (Experiment Lab and Topology are live since Phase 5B;
  // see experiment-lab.spec.ts).
  for (const route of ["/anomalies", "/performance"]) {
    await page.goto(route);
    await expect(page.getByText(/not functional yet/)).toBeVisible();
  }

  // Back on the overview, the new transfer is listed and the activity chart has rendered.
  await page.goto("/");
  await expect(page.getByRole("row", { name: /Open transfer/ }).first()).toContainText(bob);
  await expect(page.getByRole("img", { name: /Bar chart of transfers per day/ })).toBeVisible();
  await shot(page, "overview");

  expect(problems, problems.join("\n")).toEqual([]);
});

test("responsive layout at phone width", async ({ page }) => {
  const problems = trackConsole(page);
  await page.setViewportSize({ width: 390, height: 844 });
  for (const route of ["/", "/playground", "/history", "/ledger", "/experiments", "/topology"]) {
    await page.goto(route);
    await expect(page.getByRole("main")).toBeVisible();
    await page.waitForLoadState("networkidle");
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow, `horizontal page overflow on ${route}`).toBeLessThanOrEqual(0);
  }
  // Sidebar collapses into a menu on small screens.
  await expect(page.getByRole("navigation", { name: "Main" })).toBeHidden();
  await page.getByRole("button", { name: "Open navigation" }).click();
  await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
  await shot(page, "mobile-navigation");
  await page.keyboard.press("Escape");
  await page.goto("/playground");
  await shot(page, "mobile-playground");
  expect(problems, problems.join("\n")).toEqual([]);
});
