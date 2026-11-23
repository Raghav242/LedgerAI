import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import benchmark from "@/test/benchmark-20261111-130734.json";
import modelInfo from "@/test/model-info.json";
import { labRun } from "@/test/labFixtures";
import { apiError, json, mockApi, readyOk } from "@/test/mockApi";
import { TRANSFER_ID, lifecycle } from "@/test/observabilityFixtures";
import { renderApp } from "@/test/render";
import type { LabRun } from "@/types/api";
import { insights, type BenchmarkRun } from "@/features/performance/insights";

const RUN = "bbbbbbbb-1111-4bbb-8bbb-bbbbbbbbbbbb";

function scenarioRun(flagged: boolean, intentUnusual: boolean): LabRun {
  return labRun({
    id: RUN,
    experiment: "anomaly_scenario",
    status: "passed",
    params: { scenario: 2 },
    finished_at: "2026-11-20T12:00:30Z",
    result: {
      summary: flagged ? "The model flagged this transfer." : "The model did not flag this transfer (score 0.621 < threshold 0.622).",
      explanation: [],
      metrics: [],
      outcome_counts: { committed: 12 },
      data: {
        scenario: "rapid_burst",
        description: "six ~$30 transfers 4 s apart, then six more back to back",
        intent_unusual: intentUnusual,
        agrees_with_intent: flagged === intentUnusual,
        transfer_id: TRANSFER_ID,
        event_id: "e",
        result: {
          model_version: "iforest-v1",
          score: flagged ? 0.707 : 0.621,
          threshold: 0.622,
          is_anomaly: flagged,
          features: { log_amount: 3.43, amount_vs_baseline: 0, log_prior_transfers: 2.48, burst_60s: 11, log_secs_since_prev: -1.93, log_account_age: 3.24 },
          explanation: ["0.0 s since this account's previous transfer"],
          rule_flags: ["less than 1 s after the account's previous transfer"],
          inference_ms: 8.9,
          delivery_attempt: 1,
          scored_at: "2026-11-20T12:00:29Z",
          commit_to_scored_ms: 533,
        },
      },
    },
    checks: [{ id: "scored", label: "The AI worker scored the event", status: "pass", observed: "scored 533 ms after the commit", description: null }],
  });
}

function setupAi(run: LabRun, extra: Parameters<typeof mockApi>[0] = {}) {
  const api = mockApi({
    "GET /health/ready": readyOk,
    "GET /api/v1/ai/model": () => json(modelInfo),
    "GET /api/v1/ai/results": () => json([]),
    "POST /api/v1/lab/runs": () => json({ ...run, status: "queued", result: null }, { status: 202 }),
    [`GET /api/v1/lab/runs/${RUN}`]: () => json(run),
    [`GET /api/v1/observability/transfers/${TRANSFER_ID}/lifecycle`]: () => json(lifecycle()),
    ...extra,
  });
  return { api, ...renderApp("/anomalies") };
}

describe("AI Anomaly Lab", () => {
  it("runs a scenario through the normal lab API and leads with the verdict", async () => {
    const { api, user } = setupAi(scenarioRun(false, true));
    await user.click(await screen.findByRole("radio", { name: /Rapid burst/ }));
    await user.click(screen.getByRole("button", { name: /Run scenario/ }));
    await waitFor(() => expect(api.calls("POST", "/api/v1/lab/runs")).toHaveLength(1));
    expect(api.calls("POST", "/api/v1/lab/runs")[0]!.body).toEqual({ experiment: "anomaly_scenario", params: { scenario: 2 } });
    const prediction = await screen.findByTestId("ai-prediction");
    expect(within(prediction).getByText("Not flagged")).toBeInTheDocument();
    // Disagreement with the scenario's intent is shown, not hidden or "corrected".
    expect(within(prediction).getByText("Differs from the scenario's intent")).toBeInTheDocument();
    expect(within(prediction).getByText(/not a probability of fraud/)).toBeInTheDocument();
    expect(within(prediction).getByText(/less than 1 s after/)).toBeInTheDocument();
  });

  it("keeps the feature table and model card collapsed, with honest evaluation numbers", async () => {
    const { user } = setupAi(scenarioRun(true, true));
    await user.click(await screen.findByRole("button", { name: /Run scenario/ }));
    await screen.findByTestId("ai-prediction");
    expect(screen.queryByText("Typical range (5th–95th pct)")).toBeNull();
    await user.click(screen.getByRole("button", { name: /Model card/ }));
    const card = screen.getByTestId("model-card");
    expect(within(card).getByText(modelInfo.evaluation.model.f1.toFixed(3))).toBeInTheDocument();
    expect(within(card).getByText(/rule baseline scores higher/)).toBeInTheDocument();
  });

  it("explains a missing model instead of pretending", async () => {
    mockApi({ "GET /health/ready": readyOk, "GET /api/v1/ai/model": () => json({ available: false }), "GET /api/v1/ai/results": () => json([]) });
    renderApp("/anomalies");
    expect(await screen.findByText("No model artifact")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Run scenario/ })).toBeNull();
  });
});

describe("Performance Lab", () => {
  it("charts the recorded runs, labelled as recorded, with derived insights", async () => {
    mockApi({
      "GET /health/ready": readyOk,
      "GET /api/v1/performance/benchmarks": () => json({ directory_found: true, runs: [{ id: "20261111-130734", started_at: "2026-11-11T13:07:34", ...benchmark }] }),
      "GET /controller/status": () => apiError(502, "HTTP_502", "Bad gateway"),
    });
    renderApp("/performance");
    expect(await screen.findByText("Recorded results, not live", {}, { timeout: 15_000 })).toBeInTheDocument();
    expect(await screen.findByTestId("benchmark-source")).toHaveTextContent("Locust 2.47.0");
    expect(screen.getByRole("img", { name: /Throughput in requests per second/ })).toHaveAccessibleName(/1 replica: Read-heavy 307, Mixed transfers 100/);
    expect(screen.getByRole("cell", { name: "297 req/s" })).toBeInTheDocument(); // 4 replicas, mixed
    expect(screen.getByTestId("insights")).toHaveTextContent("of linear");
  });

  it("derives sub-linear scaling only from the numbers", () => {
    const run = { id: "x", started_at: null, error: null, ...benchmark } as unknown as BenchmarkRun;
    const text = insights(run).map((i) => i.text).join("\n");
    // 837 / 307 = 2.73x with 4 replicas: 68% of linear, reported as sub-linear.
    expect(text).toContain("4 replicas gave 2.73x the throughput of one (68% of linear)");
    expect(text).toContain("scaling is sub-linear");
    expect(text).not.toMatch(/linear scaling achieved/i);
  });
});
