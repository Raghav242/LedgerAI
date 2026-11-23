import { expect, test, type Page } from "@playwright/test";
import path from "node:path";

/**
 * Phase 5D failure experiments, run from the browser through the LOCAL experiment controller.
 *
 * DESTRUCTIVE (stops and restarts local containers), so OPT-IN ONLY:
 *   1. python -m labctl --enable-failures      (in another terminal)
 *   2. E2E_FAILURES=1 npx playwright test e2e/failure-lab.spec.ts
 * Without E2E_FAILURES=1 only the non-destructive availability test runs.
 */
const SHOTS = process.env.SCREENSHOTS === "1";
const shotDir = path.resolve(import.meta.dirname, "../../docs/screenshots");
const DESTRUCTIVE = process.env.E2E_FAILURES === "1";

async function shot(page: Page, name: string) {
  if (!SHOTS) return;
  const original = page.viewportSize()!;
  const height = await page.evaluate(() => document.documentElement.scrollHeight);
  await page.setViewportSize({ width: original.width, height: Math.min(height, 4000) });
  await page.waitForTimeout(200);
  await page.screenshot({ path: path.join(shotDir, `${name}.png`) });
  await page.setViewportSize(original);
}

test("failure experiments are listed and gated behind the local controller", async ({ page }) => {
  await page.goto("/experiments?experiment=redis_outage");
  const group = page.getByRole("region", { name: "Failure & recovery" });
  await expect(group.getByRole("button", { name: /Redis Outage/ })).toBeVisible();
  await expect(group.getByRole("button", { name: /Worker Crash/ })).toBeVisible();
  // Either the controller is running with failures enabled (button enabled) or the page
  // says exactly why it cannot run. It never pretends.
  const run = page.getByRole("button", { name: /Run experiment/ });
  if (await run.isDisabled()) await expect(page.getByTestId("unavailable-reason")).toContainText("labctl");
});

const RUNS: { id: string; name: RegExp; summary: RegExp; shot: string }[] = [
  { id: "redis_outage", name: /Redis Outage/, summary: /writes were refused with 503/, shot: "failure-redis" },
  { id: "rabbitmq_outage", name: /RabbitMQ Outage/, summary: /delivered exactly once/, shot: "failure-rabbitmq" },
  { id: "worker_crash", name: /Worker Crash/, summary: /really crashed twice/, shot: "failure-worker" },
  { id: "replica_failure", name: /Replica Failure/, summary: /api-1 answered all/, shot: "failure-replica" },
];

for (const r of RUNS) {
  test(`${r.id}: run from the UI, restore and verify`, async ({ page }) => {
    test.skip(!DESTRUCTIVE, "destructive: set E2E_FAILURES=1 and run python -m labctl --enable-failures");
    test.setTimeout(240_000);
    await page.goto(`/experiments?experiment=${r.id}`);
    const run = page.getByRole("button", { name: /Run experiment/ });
    await expect(run).toBeEnabled({ timeout: 15_000 });
    await run.click();
    const dialog = page.getByRole("dialog", { name: /Disturb the local stack/ });
    await dialog.getByRole("button", { name: /Run on my local stack/ }).click();
    const panel = page.getByTestId("run-panel");
    await expect(panel).toHaveAttribute("data-status", "passed", { timeout: 200_000 });
    await expect(panel.getByText(r.summary)).toBeVisible();
    await shot(page, r.shot);
    // The stack is back: the live topology shows the default components healthy again.
    await page.goto("/topology");
    await expect(page.getByTestId("topology-verdict")).toContainText(/All \d+ components healthy/, { timeout: 30_000 });
  });
}
