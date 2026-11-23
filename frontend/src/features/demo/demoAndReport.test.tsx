import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { buildReport } from "@/features/lab/report";
import { CATALOG, observation, passedConcurrencyRun, requestEvent } from "@/test/labFixtures";
import { apiError, json, mockApi, readyOk } from "@/test/mockApi";
import { renderApp } from "@/test/render";

describe("experiment report export", () => {
  it("separates observations from explanatory text and keeps the important ids", () => {
    const run = passedConcurrencyRun();
    const events = [
      requestEvent(1, observation({ replica: "api-1", request_id: "r-1" })),
      requestEvent(2, observation({ index: 1, replica: "api-2", request_id: "r-2" })),
    ];
    const report = buildReport(run, events, CATALOG[0], new Date("2026-11-20T00:00:00Z"));
    expect(report.run_id).toBe(run.id);
    expect(report.outcome).toBe("passed");
    expect(report.configuration).toEqual(run.params);
    expect(report.observed.invariant_checks).toEqual(run.checks);
    expect(report.observed.metrics).toEqual(run.result!.metrics);
    expect(report.observed.important_ids.request_ids_sample).toEqual(["r-1", "r-2"]);
    expect(report.environment.replicas_observed).toEqual(["api-1", "api-2"]);
    // Teaching text lives only in the explanatory section, labelled as not observed.
    expect(report.explanatory.note).toMatch(/Not an observation/);
    expect(report.explanatory.concept).toBe(CATALOG[0]!.concept);
    expect(JSON.stringify(report.observed)).not.toContain(CATALOG[0]!.concept);
  });
});

describe("guided demo", () => {
  function setup(runs: unknown[] = [], controller: () => Response = () => apiError(502, "HTTP_502", "Bad gateway")) {
    const api = mockApi({
      "GET /health/ready": readyOk,
      "GET /api/v1/lab/runs": () => json(runs),
      "GET /api/v1/transfers": () => json({ items: [], limit: 1, offset: 0 }),
      "GET /controller/status": controller,
    });
    return { api, ...renderApp("/demo?step=2") };
  }

  it("shows the latest RECORDED result and never starts a run itself", async () => {
    const { api } = setup([
      { id: "x", experiment: "concurrency", status: "passed", params: {}, summary: "10 of 100 succeeded.", checks_passed: 8, checks_total: 8, created_at: "2026-11-20T10:00:00Z", finished_at: "2026-11-20T10:00:05Z" },
    ]);
    const step = await screen.findByTestId("demo-step");
    expect(within(step).getByRole("heading", { name: "Concurrent withdrawals" })).toBeInTheDocument();
    expect(await within(step).findByText(/Passed .*10 of 100 succeeded/)).toBeInTheDocument();
    expect(within(step).getByText(/previously recorded, not run by this guide/)).toBeInTheDocument();
    expect(api.requests.filter((r) => r.method !== "GET")).toEqual([]);
  });

  it("locks the failure step when no local controller is available", async () => {
    const { user } = setup();
    await user.click(await screen.findByRole("button", { name: /5\. RabbitMQ outage/ }));
    expect(await screen.findByTestId("demo-locked")).toHaveTextContent("python -m labctl --enable-failures");
    expect(screen.queryByRole("link", { name: /Open the failure experiment/ })).toBeNull();
  });

  it("offers the failure step when the controller has failures enabled", async () => {
    const { user } = setup([], () =>
      json({ enabled: true, failures_enabled: true, benchmarks_enabled: false, benchmark_running: false, busy: false, active_run_id: null, experiments: [], interrupted_recovery: null }),
    );
    await user.click(await screen.findByRole("button", { name: /5\. RabbitMQ outage/ }));
    expect(await screen.findByRole("link", { name: /Open the failure experiment/ })).toHaveAttribute("href", "/experiments?experiment=rabbitmq_outage");
  });
});
