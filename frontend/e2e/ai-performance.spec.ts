import { expect, test, type Page } from "@playwright/test";
import path from "node:path";

/**
 * Phase 5E against the REAL stack (including the ai-worker). The AI scenarios create their
 * own lab accounts with simulated money. The benchmark test starts the isolated Compose
 * project ledgerflow-bench (its own containers and database) and takes minutes, so it is
 * OPT-IN: E2E_BENCHMARK=1 with
 * `python -m labctl --enable-benchmarks` running.
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

async function noOverflow(page: Page) {
  for (const width of [1024, 768, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.waitForTimeout(150);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1);
  }
  await page.setViewportSize({ width: 1440, height: 900 });
}

for (const scenario of [
  { radio: /Unusually large/, shot: "ai-large" },
  { radio: /Normal transaction/, shot: "ai-normal" },
]) {
  test(`AI scenario ${scenario.shot}: verdict arrives asynchronously from the real worker`, async ({ page }) => {
    test.setTimeout(120_000);
    await page.goto("/anomalies");
    await page.getByRole("radio", { name: scenario.radio }).click();
    await page.getByRole("button", { name: /Run scenario/ }).click();
    const prediction = page.getByTestId("ai-prediction");
    await expect(prediction).toBeVisible({ timeout: 90_000 });
    await expect(prediction).toHaveAttribute("data-anomaly", /true|false/);
    await expect(page.getByTestId("ai-summary")).toContainText(/The detector (flagged|did not flag) this transfer \(model score [\d.]+ (≥|<) threshold [\d.]+; rule baseline (fired|did not fire)\)/);
    // The event path: published and consumed by the AI worker, from persisted state.
    await prediction.getByRole("button", { name: /Event path/ }).click();
    await expect(page.locator('[data-stage="consume:anomaly-detector"]')).toHaveAttribute("data-evidence", "observed", { timeout: 20_000 });
    await shot(page, scenario.shot);
  });
}

test("performance lab charts the recorded benchmarks", async ({ page }) => {
  await page.goto("/performance");
  await expect(page.getByText("Recorded results, not live")).toBeVisible();
  await expect(page.getByRole("img", { name: /Throughput in requests per second/ })).toBeVisible();
  await expect(page.getByTestId("insights")).toContainText("of linear");
  await shot(page, "performance-lab");
  await noOverflow(page);
  await page.goto("/anomalies");
  await expect(page.getByRole("heading", { name: "AI Anomaly Lab" })).toBeVisible();
  await noOverflow(page);
});

test("a bounded benchmark started from the UI runs on the isolated database and is recorded", async ({ page }) => {
  test.skip(process.env.E2E_BENCHMARK !== "1", "starts the benchmark stack: set E2E_BENCHMARK=1 and run python -m labctl --enable-benchmarks");
  test.setTimeout(900_000);
  await page.goto("/performance");
  await expect(page.getByTestId("benchmark-source")).toBeVisible(); // lazy route loaded
  const before = await page.getByRole("combobox", { name: "Benchmark run" }).locator("option").count();
  await page.getByRole("button", { name: /Run a new benchmark/ }).click();
  await page.getByLabel("Seconds per run (10–60)").fill("10");
  await page.getByLabel("Users (8–64)").fill("16");
  await page.getByRole("button", { name: "Run benchmark" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Start" }).click();
  await expect(page.getByTestId("benchmark-running")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId("benchmark-running")).toBeHidden({ timeout: 840_000 });
  await expect(page.getByRole("combobox", { name: "Benchmark run" }).locator("option")).toHaveCount(before + 1, { timeout: 60_000 });
  await shot(page, "performance-new-run");
});
