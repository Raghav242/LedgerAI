import { expect, test, type Locator, type Page } from "@playwright/test";
import path from "node:path";

/**
 * Experiment Lab against the REAL stack (NGINX + two replicas + Redis + PostgreSQL): runs
 * every experiment from the UI, reads the result summary first, then explores the request
 * flow, checks, timeline and technical details. Screenshots with SCREENSHOTS=1.
 */
const SHOTS = process.env.SCREENSHOTS === "1";
const shotDir = path.resolve(import.meta.dirname, "../../docs/screenshots");

async function shot(page: Page, name: string) {
  if (!SHOTS) return;
  const original = page.viewportSize()!;
  const height = await page.evaluate(() => document.documentElement.scrollHeight);
  await page.setViewportSize({ width: original.width, height: Math.min(height, 5000) });
  await page.waitForTimeout(400); // let the one-time flow animation finish
  await page.screenshot({ path: path.join(shotDir, `${name}.png`) });
  await page.setViewportSize(original);
}

function trackConsole(page: Page) {
  const problems: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error" || msg.type() === "warning") problems.push(`${msg.type()}: ${msg.text()}`);
  });
  page.on("pageerror", (err) => problems.push(`pageerror: ${err.message}`));
  return problems;
}

async function selectExperiment(page: Page, name: string, title: string): Promise<Locator> {
  await page.getByRole("list", { name: "Experiments", exact: true }).getByRole("button", { name: new RegExp(name) }).click();
  return page.getByRole("form", { name: `${title} parameters` });
}

async function runExperiment(page: Page, name: string, title: string, params: Record<string, string> = {}): Promise<Locator> {
  const form = await selectExperiment(page, name, title);
  for (const [label, value] of Object.entries(params)) await form.getByLabel(label, { exact: true }).fill(value);
  const run = form.getByRole("button", { name: "Run experiment" });
  await expect(run).toBeEnabled({ timeout: 30_000 }); // a previous run may still be finishing
  await run.click();
  const panel = page.getByTestId("run-panel");
  await expect(panel).toHaveAttribute("data-status", /queued|running|passed/);
  await expect(panel).toHaveAttribute("data-status", "passed", { timeout: 90_000 });
  return panel;
}

async function expectChecksPass(page: Page, labels: string[]) {
  await page.getByRole("tab", { name: /Checks/ }).click();
  const list = page.getByRole("list", { name: "Experiment invariants" });
  for (const label of labels) await expect(list.getByRole("listitem").filter({ hasText: label })).toHaveAttribute("data-status", "pass");
  await page.getByRole("tab", { name: /Request flow/ }).click();
}

async function noHorizontalOverflow(page: Page, where: string) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow, `horizontal page overflow on ${where}`).toBeLessThanOrEqual(0);
}

test("run every experiment from the UI against the real stack", async ({ page }) => {
  test.setTimeout(300_000);
  const problems = trackConsole(page);

  await page.goto("/experiments");
  await expect(page.getByRole("heading", { name: "Experiment Lab" })).toBeVisible();
  await expect(page.getByRole("list", { name: "Experiments", exact: true }).getByRole("button")).toHaveCount(4);
  const form = await selectExperiment(page, "Concurrency", "Concurrent withdrawals");
  await expect(form.getByLabel("Amount", { exact: true })).toHaveValue("10.00");
  await shot(page, "lab-selection");
  await page.getByRole("button", { name: "How this works" }).click();
  await expect(page.getByText("Expected behaviour")).toBeVisible();
  await shot(page, "lab-how-it-works");
  await page.getByRole("button", { name: "How this works" }).click();

  // --- Concurrency (defaults: 100 x $10 from $100) ---------------------------------------------
  await runExperiment(page, "Concurrency", "Concurrent withdrawals");
  await expect(page.getByTestId("run-summary")).toContainText("10 succeeded, 90 were rejected for insufficient funds");
  const keyResults = page.getByLabel("Key results");
  await expect(keyResults.locator('[data-metric="succeeded"]')).toContainText("10");
  await expect(keyResults.locator('[data-metric="rejected"]')).toContainText("90");
  await expect(keyResults.locator('[data-metric="unexpected"]')).toContainText("0");
  const flow = page.getByRole("figure", { name: "Request flow" });
  await expect(flow.locator('[data-node="database"]')).toHaveAttribute("data-state", "blocked"); // 90 of 100 blocked there
  await expect(flow.locator('[data-node="replica"]')).toContainText("2 replicas");
  await shot(page, "lab-concurrency");

  // Explore: a component, all checks, the timeline, technical details.
  await flow.locator('[data-node="database"]').click();
  const details = page.getByRole("region", { name: "PostgreSQL details" });
  await expect(details.locator('[data-stage="postgres"]')).toContainText("blocked");
  await page.getByRole("button", { name: "Understand this result" }).click();
  await expect(page.getByTestId("guided-explanation").getByText("Why does it work?")).toBeVisible();
  await shot(page, "lab-concurrency-explore");
  await page.getByRole("button", { name: "Understand this result" }).click();

  await expectChecksPass(page, ["Exactly 10 transfers succeeded", "Source balance never negative", "Global reconciliation passes"]);
  await page.getByRole("tab", { name: /Timeline/ }).click();
  await expect(page.getByRole("list", { name: "Run timeline" })).toContainText("Creating two isolated lab accounts");
  await page.getByRole("tab", { name: /Technical details/ }).click();
  await expect(page.getByTestId("result-concurrency").getByRole("table", { name: "Outcomes per replica" })).toBeVisible();
  await page.getByRole("tab", { name: /Request flow/ }).click();

  // One request's path, picked from the outcome toolbar.
  await page.getByRole("toolbar", { name: "Flow view" }).getByRole("button", { name: /Committed \(201\)/ }).click();
  await expect(flow.locator('[data-node="database"]')).toHaveAttribute("data-state", "success", { timeout: 5_000 });
  await expect(flow.locator('[data-node="replica"]')).toContainText(/api-\d/);
  await expect(page.getByTestId("selected-request")).toContainText("201");

  // --- Idempotency ------------------------------------------------------------------------------
  await runExperiment(page, "Idempotency", "Idempotent retries");
  await expect(page.getByLabel("Key results").locator('[data-metric="committed"]')).toContainText("1");
  await expect(flow.locator('[data-node="database"]')).toHaveAttribute("data-state", "replayed");
  await expectChecksPass(page, ["Exactly one request executed the transfer", "All other responses were replays", "Exactly one transfer recorded in PostgreSQL"]);
  await shot(page, "lab-idempotency");

  // --- Rate limiting -----------------------------------------------------------------------------
  await runExperiment(page, "Rate Limiting", "Distributed rate limiting");
  await expect(flow.locator('[data-node="redis"]')).toContainText("blocked");
  await expectChecksPass(page, [
    "Global quota respected during the burst",
    "Every 429 carries a Retry-After header",
    "Admitted requests were served by several replicas",
    "Independent per-replica counters would have over-admitted",
  ]);
  await page.getByRole("tab", { name: /Technical details/ }).click();
  await expect(page.getByTestId("result-rate-limit").getByText("Per-replica counters (counterfactual)")).toBeVisible();
  await page.getByRole("tab", { name: /Request flow/ }).click();
  await shot(page, "lab-rate-limiting");

  // --- Load balancing ---------------------------------------------------------------------------
  await runExperiment(page, "Load Balancing", "Load balancing", { Requests: "30" });
  const bars = page.getByRole("list", { name: "Requests per replica" });
  await expect(bars.locator('[data-replica="api-1"]')).toBeVisible();
  await expect(bars.locator('[data-replica="api-2"]')).toBeVisible();
  await expectChecksPass(page, ["Every request succeeded", "Traffic reached more than one replica", "Traffic is evenly split"]);
  await shot(page, "lab-load-balancing");

  // Runs survive a reload (stored in PostgreSQL).
  await page.reload();
  await expect(page.getByTestId("run-panel")).toHaveAttribute("data-status", "passed");
  await expect(page.getByRole("list", { name: "Recent runs" }).getByRole("button")).not.toHaveCount(0);

  // --- Topology: illustrated flows (behind a disclosure, below the live view) ------------------
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: /System Topology/ }).click();
  await expect(page.getByRole("heading", { name: "System Topology" })).toBeVisible();
  await page.getByRole("button", { name: /How requests travel/ }).click();
  await page.getByRole("tab", { name: "Rate-limit rejection" }).click();
  await expect(page.getByRole("figure", { name: "Rate-limit rejection flow" }).locator('[data-node="redis"]')).toHaveAttribute("data-state", "blocked");
  await page.getByRole("tab", { name: "Transfer" }).click();
  const transfer = page.getByRole("figure", { name: "Transfer flow" });
  await expect(transfer.getByText("Following the request…")).toHaveCount(0);
  await transfer.locator('[data-node="redis"]').click();
  await expect(page.getByRole("region", { name: "Redis details" }).getByText("Illustration", { exact: true })).toBeVisible();
  await shot(page, "topology");

  expect(problems).toEqual([]);
});

for (const [label, width, height] of [
  ["laptop", 1024, 768],
  ["tablet", 768, 1024],
  ["phone", 390, 844],
] as const) {
  test(`experiment lab layout at ${label} width (${width}px)`, async ({ page }) => {
    const problems = trackConsole(page);
    await page.setViewportSize({ width, height });
    await page.goto("/experiments");
    await expect(page.getByRole("heading", { name: "Experiment Lab" })).toBeVisible();
    // Open the newest run (left by the main test, or any earlier run).
    const historyToggle = page.getByRole("button", { name: /Run history/ });
    if ((await historyToggle.getAttribute("aria-expanded")) === "false") await historyToggle.click();
    const firstRun = page.getByRole("list", { name: "Recent runs" }).getByRole("button").first();
    await firstRun.click();
    await expect(page.getByTestId("run-panel")).toBeVisible();
    await page.waitForLoadState("networkidle");
    await noHorizontalOverflow(page, `/experiments at ${width}px`);
    // The primary action stays reachable.
    await expect(page.getByRole("button", { name: "Run experiment" })).toBeVisible();
    await page.getByRole("figure", { name: "Request flow" }).locator('[data-node="redis"]').click();
    await expect(page.getByTestId("flow-node-details")).toBeVisible();
    await noHorizontalOverflow(page, `/experiments with details at ${width}px`);
    await shot(page, `lab-${label}`);
    await page.goto("/topology");
    await noHorizontalOverflow(page, `/topology at ${width}px`);
    expect(problems).toEqual([]);
  });
}
