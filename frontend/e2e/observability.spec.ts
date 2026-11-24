import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import path from "node:path";

/**
 * Phase 5C against the REAL stack: live topology, the request lifecycle explorer and the event
 * pipeline. Creates its own `E2E obs …` accounts with simulated money; controls no infrastructure.
 * Screenshots go to ../docs/screenshots when SCREENSHOTS=1.
 */
const SHOTS = process.env.SCREENSHOTS === "1";
const shotDir = path.resolve(import.meta.dirname, "../../docs/screenshots");

async function shot(page: Page, name: string) {
  if (!SHOTS) return;
  const original = page.viewportSize()!;
  const height = await page.evaluate(() => document.documentElement.scrollHeight);
  await page.setViewportSize({ width: original.width, height: Math.min(height, 4000) });
  await page.waitForTimeout(200);
  await page.screenshot({ path: path.join(shotDir, `${name}.png`) });
  await page.setViewportSize(original);
}

async function noHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(1);
}

async function apiTransfer(request: APIRequestContext, run: string) {
  const make = async (name: string) => (await (await request.post("/api/v1/accounts", { data: { name } })).json()).id as string;
  const a = await make(`E2E obs ${run} A`);
  const b = await make(`E2E obs ${run} B`);
  expect((await request.post(`/api/v1/accounts/${a}/fund`, { data: { amount_minor: 10_000 } })).ok()).toBeTruthy();
  const response = await request.post("/api/v1/transfers", {
    data: { source_account_id: a, destination_account_id: b, amount_minor: 1_234, currency: "USD" },
    headers: { "Idempotency-Key": `e2e-obs-${run}` },
  });
  expect(response.status()).toBe(201);
  return (await response.json()).id as string;
}

test("live topology shows observed component health with details", async ({ page }) => {
  await page.goto("/topology");
  await expect(page.getByRole("heading", { name: "System Topology" })).toBeVisible();
  const verdict = page.getByTestId("topology-verdict");
  await expect(verdict).toContainText(/components (healthy|need attention)/);
  for (const id of ["browser", "nginx", "api-1", "api-2", "redis", "postgres", "outbox-publisher", "rabbitmq", "event-consumer"]) {
    await expect(page.locator(`[data-component="${id}"]`)).toBeVisible();
  }
  // In the default stack these are genuinely observed healthy right now.
  for (const id of ["nginx", "api-1", "api-2", "redis", "postgres", "outbox-publisher", "event-consumer", "rabbitmq"]) {
    await expect(page.locator(`[data-component="${id}"]`)).toHaveAttribute("data-status", "healthy");
  }
  await page.locator('[data-component="rabbitmq"]').click();
  const details = page.getByTestId("component-details");
  // Phase 6A: with RABBITMQ_MONITOR_PASSWORD set, the broker is probed independently through the
  // management API (least-privilege monitoring user); without it, health is derived from workers.
  await expect(details.getByText(/^(Probed|Derived)$/)).toBeVisible();
  await expect(
    details.getByText(/management API, probed by|connection of (event-consumer|outbox-publisher|anomaly-detector)/),
  ).toBeVisible();
  await page.locator('[data-component="postgres"]').click();
  await expect(details.getByText("Probed", { exact: true })).toBeVisible();
  await shot(page, "topology-live");

  for (const width of [1024, 768, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.waitForTimeout(200);
    await noHorizontalOverflow(page);
  }
  await page.setViewportSize({ width: 390, height: 900 });
  await expect(page.getByRole("region", { name: "Event path (after COMMIT)" })).toBeVisible();
  await shot(page, "topology-phone");
});

test("event pipeline follows a real transfer to its consumer", async ({ page, request }) => {
  const run = Date.now().toString(36);
  const transferId = await apiTransfer(request, run);
  await page.goto("/events");
  await expect(page.getByRole("heading", { name: "Event Pipeline" })).toBeVisible();
  await expect(page.getByTestId("pipeline-verdict")).toBeVisible();
  const row = page.locator("tr[data-event]").filter({ hasText: "$12.34" }).first();
  await expect(row).toBeVisible();
  await row.getByRole("button", { name: "transfer.completed" }).click();
  const sheet = page.getByRole("dialog", { name: "Follow one event" });
  // Published and consumed: observed from persisted state (the consumer is running).
  await expect(sheet.locator('[data-stage="publish"]')).toHaveAttribute("data-evidence", "observed", { timeout: 20_000 });
  await expect(sheet.locator('[data-stage="consume:ledger-audit"]')).toHaveAttribute("data-evidence", "observed", { timeout: 20_000 });
  // Not received by this page: replica unknown, response derived — never invented.
  await expect(sheet.locator('[data-stage="response"]')).toHaveAttribute("data-evidence", "derived");
  await shot(page, "event-pipeline");
  await page.keyboard.press("Escape");
  for (const width of [1024, 768, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.waitForTimeout(150);
    await noHorizontalOverflow(page);
  }
  expect(transferId).toBeTruthy();
});

test("history drawer shows the lifecycle of a committed transfer", async ({ page, request }) => {
  const run = `${Date.now().toString(36)}h`;
  await apiTransfer(request, run);
  await page.goto("/history");
  const firstRow = page.getByRole("row", { name: /Open transfer/ }).first();
  await expect(firstRow).toContainText(`E2E obs ${run} A`);
  await firstRow.click();
  const sheet = page.getByRole("dialog", { name: "Transfer details" });
  await sheet.getByRole("button", { name: /Lifecycle/ }).click();
  await expect(sheet.locator('[data-stage="database"]')).toHaveAttribute("data-evidence", "observed");
  await expect(sheet.locator('[data-stage="consume:ledger-audit"]')).toHaveAttribute("data-evidence", "observed", { timeout: 20_000 });
  await expect(sheet.locator('[data-stage="replica"]')).toHaveAttribute("data-evidence", /observed|unknown/);
  await shot(page, "history-lifecycle");
});
