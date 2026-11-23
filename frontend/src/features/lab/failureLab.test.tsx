import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CATALOG, RUN_ID, labRun } from "@/test/labFixtures";
import { apiError, json, mockApi, readyOk } from "@/test/mockApi";
import { renderApp } from "@/test/render";
import type { ExperimentDefinition, LabRun } from "@/types/api";

const REDIS: ExperimentDefinition = {
  id: "redis_outage",
  title: "Redis outage",
  summary: "Stop Redis: financial writes fail closed, nothing changes, then recover.",
  concept: "Fail-closed writes, fail-open reads, and clean recovery",
  expected: ["While Redis is down, every transfer gets 503 RATE_LIMITER_UNAVAILABLE, fast."],
  how_it_works: ["docker compose stop redis (allow-listed), then sends the probe writes."],
  risk: "infrastructure",
  runner: "controller",
  risk_note: "Stops and restarts ONE allow-listed service.",
  available: false,
  unavailable_reason: "Infrastructure experiments run only through the local experiment controller: python -m labctl --enable-failures",
  estimated_seconds: 15,
  params: [
    { name: "probe_writes", label: "Writes during the outage", description: "Transfers attempted while Redis is down.", unit: "requests", default: 3, minimum: 1, maximum: 5 },
    { name: "outage_seconds", label: "Outage length", description: "How long Redis stays stopped.", unit: "seconds", default: 3, minimum: 1, maximum: 15 },
  ],
};

const CONTROLLER_READY = { enabled: true, failures_enabled: true, busy: false, active_run_id: null, experiments: ["redis_outage"], interrupted_recovery: null };

function passedRedisRun(): LabRun {
  return labRun({
    experiment: "redis_outage",
    status: "passed",
    params: { probe_writes: 3, outage_seconds: 3 },
    runner_replica: "lab-controller",
    finished_at: "2026-11-20T12:00:10Z",
    result: {
      summary: "With Redis down, 3 of 3 writes were refused with 503 in at most 259 ms and nothing changed.",
      explanation: ["The write rate limit is a hard dependency."],
      metrics: [
        { key: "refused", label: "Writes refused (503)", value: 3, unit: null, tone: "warning", hint: null },
        { key: "failure_latency", label: "Slowest refusal", value: 259, unit: "ms", tone: "neutral", hint: null },
        { key: "recovery", label: "Recovery after restart", value: 0.2, unit: "s", tone: "neutral", hint: null },
        { key: "reads", label: "Read during outage", value: 200, unit: null, tone: "neutral", hint: null },
      ],
      outcome_counts: { committed: 2, limiter_unavailable: 3, read_ok: 1, replayed: 1 },
      data: {
        component: "redis",
        component_status: [
          { phase: "before", status: "healthy" },
          { phase: "during", status: "unhealthy" },
          { phase: "after", status: "healthy" },
        ],
      },
    },
    checks: [{ id: "fail_closed", label: "Every write during the outage got 503 RATE_LIMITER_UNAVAILABLE", status: "pass", observed: "503 ×3", description: null }],
  });
}

function setup(routes: Parameters<typeof mockApi>[0]) {
  const api = mockApi({
    "GET /health/ready": readyOk,
    "GET /api/v1/lab/experiments": () => json([...CATALOG, REDIS]),
    "GET /api/v1/lab/runs": () => json([]),
    ...routes,
  });
  return { api, ...renderApp("/experiments?experiment=redis_outage") };
}

describe("failure experiments in the lab", () => {
  it("lists them separately and explains how to enable them when no controller runs", async () => {
    setup({ "GET /controller/status": () => apiError(502, "HTTP_502", "Bad gateway") });
    const group = await screen.findByRole("region", { name: "Failure & recovery" });
    expect(within(group).getByText("Redis Outage")).toBeInTheDocument();
    expect(await screen.findByTestId("unavailable-reason")).toHaveTextContent("python -m labctl --enable-failures");
    expect(screen.getByRole("button", { name: /Run experiment/ })).toBeDisabled();
  });

  it("stays disabled when the controller runs without --enable-failures", async () => {
    setup({ "GET /controller/status": () => json({ ...CONTROLLER_READY, failures_enabled: false, experiments: [] }) });
    expect(await screen.findByText(/Restart it with --enable-failures/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Run experiment/ })).toBeDisabled();
  });

  it("asks for confirmation, then starts the run through the controller (never the API)", async () => {
    const { api, user } = setup({
      "GET /controller/status": () => json(CONTROLLER_READY),
      "POST /controller/runs": () => json({ id: RUN_ID, experiment: "redis_outage", status: "queued" }, { status: 202 }),
      [`GET /api/v1/lab/runs/${RUN_ID}`]: () => json(passedRedisRun()),
      [`GET /api/v1/lab/runs/${RUN_ID}/events`]: () => json({ items: [], next_after: 0, run_status: "passed", event_count: 0, events_dropped: 0 }),
    });
    const run = await screen.findByRole("button", { name: /Run experiment/ });
    await waitFor(() => expect(run).toBeEnabled());
    await user.click(run);
    const dialog = await screen.findByRole("dialog", { name: /Disturb the local stack/ });
    expect(dialog).toHaveTextContent("stop the local Redis container");
    expect(api.calls("POST", "/controller/runs")).toHaveLength(0); // nothing before confirmation
    await user.click(within(dialog).getByRole("button", { name: /Run on my local stack/ }));
    await waitFor(() => expect(api.calls("POST", "/controller/runs")).toHaveLength(1));
    expect(api.calls("POST", "/controller/runs")[0]!.body).toEqual({ experiment: "redis_outage", params: { probe_writes: 3, outage_seconds: 3 }, confirm: true });
    expect(api.calls("POST", "/api/v1/lab/runs")).toHaveLength(0);

    // The run is then followed through the normal API, with the before/during/after strip.
    const strip = await screen.findByTestId("phase-status");
    expect(within(strip).getByText("Unhealthy")).toBeInTheDocument();
    expect(screen.getByText(/3 of 3 writes were refused/)).toBeInTheDocument();
  });
});
