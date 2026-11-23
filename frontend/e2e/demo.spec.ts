import { expect, test, type Page } from "@playwright/test";
import path from "node:path";

/** The guided demo against the real backend: navigation only, it never runs anything. */
const SHOTS = process.env.SCREENSHOTS === "1";
const shotDir = path.resolve(import.meta.dirname, "../../docs/screenshots");

async function shot(page: Page, name: string) {
  if (!SHOTS) return;
  await page.waitForTimeout(200);
  await page.screenshot({ path: path.join(shotDir, `${name}.png`), fullPage: false });
}

test("overview leads into the guided demo, which only links and reports", async ({ page }) => {
  const writes: string[] = [];
  page.on("request", (r) => {
    if (r.method() !== "GET" && r.url().includes("/api/")) writes.push(`${r.method()} ${r.url()}`);
  });
  await page.goto("/");
  await page.getByRole("link", { name: /Start guided demo/ }).click();
  const step = page.getByTestId("demo-step");
  await expect(step.getByRole("heading", { name: "A basic transfer" })).toBeVisible();
  await expect(step.getByTestId("demo-observed")).toContainText(/Most recent transfer|Not run recently/);
  await page.getByRole("button", { name: "Next" }).click();
  await expect(step.getByRole("heading", { name: "Concurrent withdrawals" })).toBeVisible();
  await expect(step.getByTestId("demo-observed")).toContainText("previously recorded, not run by this guide");
  await page.getByRole("button", { name: /7\. Performance comparison/ }).click();
  await expect(step.getByTestId("demo-observed")).toContainText(/Recorded/);
  await page.getByRole("button", { name: /2\. Concurrent withdrawals/ }).click();
  await shot(page, "guided-demo");
  for (const width of [1024, 768, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.waitForTimeout(150);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1);
  }
  expect(writes).toEqual([]);
});
