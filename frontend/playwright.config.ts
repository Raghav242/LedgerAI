import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end tests against the REAL local backend (docker compose stack on :8080), through
 * the Vite dev server and its proxy. They create uniquely named accounts with simulated
 * money in the development database and never touch infrastructure.
 *
 * Uses an installed browser (Edge by default; PW_CHANNEL=chrome for Chrome) so no browser
 * download is needed: npx playwright install is optional.
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:5173",
    channel: process.env.PW_CHANNEL ?? "msedge",
    trace: "retain-on-failure",
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Edge"], viewport: { width: 1440, height: 900 } } },
  ],
  webServer: {
    command: "npm run dev",
    url: "http://127.0.0.1:5173",
    reuseExistingServer: true,
    timeout: 60_000,
  },
});
