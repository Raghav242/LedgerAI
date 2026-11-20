import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CATALOG, RUN_ID, labRun, observation, passedConcurrencyRun, requestEvent } from "@/test/labFixtures";
import { apiError, json, mockApi, readyOk } from "@/test/mockApi";
import { renderApp } from "@/test/render";
import type { LabEvent, LabRun } from "@/types/api";

type Routes = Parameters<typeof mockApi>[0];

const POLL_TIMEOUT = { timeout: 5_000 };

function eventsPage(items: LabEvent[], run: Pick<LabRun, "status" | "event_count">) {
  return json({
    items,
    next_after: items.at(-1)?.seq ?? 0,
    run_status: run.status,
    event_count: run.event_count,
    events_dropped: 0,
  });
}

function setup(routes: Routes = {}, path = "/experiments") {
  const api = mockApi({
    "GET /health/ready": readyOk,
    "GET /api/v1/lab/experiments": () => json(CATALOG),
    "GET /api/v1/lab/runs": () => json([]),
    ...routes,
  });
  return { api, ...renderApp(path) };
}

const CONCURRENCY_EVENTS: LabEvent[] = [
  { seq: 1, t_ms: 0, kind: "phase", level: "info", title: "Creating two isolated lab accounts", detail: null, phase: "setup", request: null },
  requestEvent(2, observation({ index: 0, outcome: "committed", replica: "api-1" })),
  requestEvent(3, observation({ index: 1, status: 409, code: "INSUFFICIENT_FUNDS", outcome: "insufficient_funds", replica: "api-2" })),
  requestEvent(4, observation({ index: 2, status: 409, code: "INSUFFICIENT_FUNDS", outcome: "insufficient_funds", replica: "api-1" })),
];

describe("experiment selection and configuration", () => {
  it("shows compact experiment cards and the selected experiment's essentials only", async () => {
    setup();
    const list = await screen.findByRole("list", { name: "Experiments" });
    const cards = within(list).getAllByRole("button");
    expect(cards.map((c) => c.getAttribute("data-experiment"))).toEqual(["concurrency", "idempotency", "rate_limiting", "load_balancing"]);
    for (const [name, tagline] of [
      ["Concurrency", "Test simultaneous withdrawals"],
      ["Idempotency", "Prevent duplicate transfers"],
      ["Rate Limiting", "Control requests across replicas"],
      ["Load Balancing", "Distribute traffic across servers"],
    ]) {
      expect(within(list).getByText(name!)).toBeInTheDocument();
      expect(within(list).getByText(tagline!)).toBeInTheDocument();
    }
    expect(cards[0]).toHaveAttribute("aria-pressed", "true");

    // Title, one-sentence purpose, parameters and the primary action are visible...
    expect(screen.getByRole("heading", { name: "Concurrent withdrawals" })).toBeInTheDocument();
    expect(screen.getByText("Can many simultaneous withdrawals ever overspend one balance?")).toBeInTheDocument();
    const form = screen.getByRole("form", { name: "Concurrent withdrawals parameters" });
    expect(within(form).getByLabelText("Requests")).toHaveValue("100");
    expect(within(form).getByLabelText("Amount")).toHaveValue("10.00"); // dollars, not 1000 cents
    expect(within(form).getByText("$0.01–$1,000.00")).toBeInTheDocument();
    expect(within(form).getByRole("button", { name: "Run experiment" })).toBeEnabled();
    // ...while the long explanation is collapsed.
    expect(screen.queryByText("Expected behaviour")).toBeNull();
  });

  it("expands 'How this works' with expectations, steps and parameter explanations", async () => {
    const { user } = setup();
    const toggle = await screen.findByRole("button", { name: "How this works" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Expected behaviour")).toBeInTheDocument();
    expect(screen.getByText(/Exactly funding ÷ amount transfers succeed/)).toBeInTheDocument();
    expect(screen.getByText(/Row locking \(SELECT … FOR UPDATE\)/)).toBeInTheDocument();
    expect(screen.getByText("Concurrent requests:")).toBeInTheDocument();
  });

  it("switching experiments shows that experiment's own parameters", async () => {
    const { user, router } = setup();
    await user.click(await screen.findByRole("button", { name: /Rate Limiting/ }));
    const form = await screen.findByRole("form", { name: "Distributed rate limiting parameters" });
    expect(within(form).getByLabelText("Burst size")).toHaveValue("50");
    expect(within(form).getByLabelText("Refill wait")).toHaveValue("2");
    expect(router.state.location.search).toContain("experiment=rate_limiting");
  });

  it("keeps the safety note one click away", async () => {
    const { user } = setup();
    const safety = await screen.findByRole("button", { name: "Safe · local only" });
    await user.click(safety);
    expect(screen.getByText(/Nothing here stops containers, injects failures or runs commands/)).toBeInTheDocument();
  });

  it("explains how to enable the lab when the backend does not expose it", async () => {
    setup({ "GET /api/v1/lab/experiments": () => apiError(404, "NOT_FOUND", "Not Found") });
    expect(await screen.findByText("The Experiment Lab is disabled on this backend")).toBeInTheDocument();
    expect(screen.getByText(/ENABLE_DEV_ENDPOINTS=true/)).toBeInTheDocument();
  });
});

describe("running an experiment", () => {
  it("validates parameters locally and never sends an out-of-range run", async () => {
    const { api, user } = setup({ "POST /api/v1/lab/runs": () => json(labRun(), { status: 202 }) });
    const form = await screen.findByRole("form", { name: "Concurrent withdrawals parameters" });
    const input = within(form).getByLabelText("Requests");
    await user.clear(input);
    await user.type(input, "500");
    const amount = within(form).getByLabelText("Amount");
    await user.clear(amount);
    await user.type(amount, "5000");
    await user.click(within(form).getByRole("button", { name: "Run experiment" }));
    expect(await within(form).findByText("Must be between 2 and 200")).toBeInTheDocument();
    expect(within(form).getByText("Must be between $0.01 and $1,000.00")).toBeInTheDocument();
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(api.calls("POST", "/api/v1/lab/runs")).toHaveLength(0);
  });

  it("submits, polls progress, then leads with the result and offers details on demand", async () => {
    const finished = passedConcurrencyRun();
    const { api, user, router } = setup({
      "POST /api/v1/lab/runs": () => json(labRun({ status: "queued", progress: { phase: "queued", message: "Waiting for the runner", completed: 0, total: 0 } }), { status: 202 }),
      "GET /api/v1/lab/runs/:id": [() => json(labRun()), () => json(labRun()), () => json(finished)],
      "GET /api/v1/lab/runs/:id/events": [
        () => eventsPage(CONCURRENCY_EVENTS.slice(0, 2), { status: "running", event_count: 2 }),
        (req) => {
          const after = Number(req.query.get("after"));
          return eventsPage(
            CONCURRENCY_EVENTS.filter((e) => e.seq > after),
            finished,
          );
        },
      ],
    });
    const form = await screen.findByRole("form", { name: "Concurrent withdrawals parameters" });
    await user.click(within(form).getByRole("button", { name: "Run experiment" }));

    await waitFor(() => expect(api.calls("POST", "/api/v1/lab/runs")).toHaveLength(1));
    expect(api.calls("POST", "/api/v1/lab/runs")[0]!.body).toEqual({
      experiment: "concurrency",
      params: { requests: 100, amount_minor: 1000, funding_minor: 10000 }, // integer cents on the wire
    });
    await waitFor(() => expect(router.state.location.search).toContain(`run=${RUN_ID}`));

    // Queued first (the POST response), then progress from the next poll.
    const progress = await screen.findByTestId("run-progress");
    expect(within(progress).getByText("Waiting for the runner")).toBeInTheDocument();
    expect(await within(progress).findByText("40/100", {}, POLL_TIMEOUT)).toBeInTheDocument();
    expect(within(progress).getByRole("progressbar", { name: "Run progress" })).toHaveAttribute("aria-valuenow", "40");

    // Finished: status, one sentence, key numbers and one picture come first.
    await waitFor(() => expect(screen.getByTestId("run-status")).toHaveAttribute("data-status", "passed"), POLL_TIMEOUT);
    expect(screen.queryByTestId("run-progress")).toBeNull();
    expect(screen.getByTestId("run-summary")).toHaveTextContent("10 succeeded, 90 were rejected for insufficient funds");
    const keyResults = screen.getByLabelText("Key results");
    expect(within(keyResults).getByText("Succeeded").parentElement).toHaveTextContent("10");
    expect(within(keyResults).getByText("Insufficient funds").parentElement).toHaveTextContent("90");
    expect(screen.getByRole("img", { name: "Final outcomes: 10 succeeded, 90 rejected: insufficient funds" })).toBeInTheDocument();
    // The guided explanation is collapsed after a pass.
    expect(screen.getByRole("button", { name: "Understand this result" })).toHaveAttribute("aria-expanded", "false");

    // Request flow is the default tab: aggregate over observed requests (1 committed + 2 blocked).
    const flow = screen.getByRole("figure", { name: "Request flow" });
    const database = flow.querySelector('[data-node="database"]') as HTMLElement;
    expect(database).toHaveAttribute("data-state", "blocked");
    expect(database).toHaveTextContent("1 passed");
    expect(database).toHaveTextContent("2 blocked");

    // Checks tab: summary line plus expandable rows.
    await user.click(screen.getByRole("tab", { name: /Checks/ }));
    expect(screen.getByTestId("invariant-summary")).toHaveTextContent("3 of 3 checks passed");
    const invariants = screen.getByRole("list", { name: "Experiment invariants" });
    const exact = within(invariants).getByRole("button", { name: /Exactly 10 transfers succeeded/ });
    expect(exact.closest("li")).toHaveAttribute("data-status", "pass");
    expect(within(invariants).queryByText("10 succeeded")).toBeNull();
    await user.click(exact);
    expect(exact).toHaveAttribute("aria-expanded", "true");
    expect(within(invariants).getByText("10 succeeded")).toBeInTheDocument();

    // Timeline tab: clicking a request shows its path in the flow tab.
    await user.click(screen.getByRole("tab", { name: /Timeline/ }));
    const timeline = await screen.findByRole("list", { name: "Run timeline" });
    expect(within(timeline).getByText("Creating two isolated lab accounts")).toBeInTheDocument();
    await user.click(within(timeline).getByRole("button", { name: /Show the path of #2 POST → 409 INSUFFICIENT_FUNDS/ }));
    expect(screen.getByRole("tab", { name: /Request flow/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("selected-request")).toHaveTextContent("Request #2 · POST /api/v1/transfers · 409 INSUFFICIENT_FUNDS · api-2");
    const single = screen.getByRole("figure", { name: "Request flow" });
    expect(single.querySelector('[data-node="replica"]')).toHaveTextContent("api-2");
    expect(single.querySelector('[data-node="database"]')).toHaveAttribute("data-state", "blocked");

    // Technical details tab keeps every metric and the per-replica breakdown.
    await user.click(screen.getByRole("tab", { name: /Technical details/ }));
    expect(within(screen.getByLabelText("Run metrics")).getByText("Total requests")).toBeInTheDocument();
    expect(within(screen.getByTestId("result-concurrency")).getByRole("table", { name: "Outcomes per replica" })).toBeInTheDocument();
    expect(screen.getByText("api-1 → nginx:8080")).toBeInTheDocument();
  });

  it("tabs are keyboard navigable", async () => {
    const { user } = setup(
      {
        "GET /api/v1/lab/runs/:id": () => json(passedConcurrencyRun()),
        "GET /api/v1/lab/runs/:id/events": () => eventsPage(CONCURRENCY_EVENTS, { status: "passed", event_count: 4 }),
      },
      `/experiments?run=${RUN_ID}`,
    );
    const flowTab = await screen.findByRole("tab", { name: /Request flow/ });
    flowTab.focus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: /Checks/ })).toHaveFocus();
    expect(screen.getByRole("tab", { name: /Checks/ })).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{End}");
    expect(screen.getByRole("tab", { name: /Technical details/ })).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: /Request flow/ })).toHaveAttribute("aria-selected", "true");
  });

  it("the guided explanation follows the same four questions and uses the run's own result", async () => {
    const { user } = setup(
      {
        "GET /api/v1/lab/runs/:id": () => json(passedConcurrencyRun()),
        "GET /api/v1/lab/runs/:id/events": () => eventsPage([], { status: "passed", event_count: 0 }),
      },
      `/experiments?run=${RUN_ID}`,
    );
    await user.click(await screen.findByRole("button", { name: "Understand this result" }));
    const guide = screen.getByTestId("guided-explanation");
    for (const heading of ["What are we testing?", "What happened?", "Why does it work?", "Explore the implementation"]) {
      expect(within(guide).getByRole("heading", { name: heading })).toBeInTheDocument();
    }
    expect(within(guide).getByText(/10 succeeded, 90 were rejected/)).toBeInTheDocument();
    expect(within(guide).getByText("Only 10 can fit.")).toBeInTheDocument();
  });

  it("explains a 409 when another run is already active", async () => {
    const { user } = setup({
      "POST /api/v1/lab/runs": () =>
        apiError(409, "LAB_RUN_IN_PROGRESS", "Another experiment is already running; runs never overlap.", {
          active_run_id: RUN_ID,
          active_experiment: "rate_limiting",
        }),
    });
    const form = await screen.findByRole("form", { name: "Concurrent withdrawals parameters" });
    await user.click(within(form).getByRole("button", { name: "Run experiment" }));
    expect(await screen.findByText("Another run is in progress")).toBeInTheDocument();
  });

  it("disables running while another run is active", async () => {
    setup({
      "GET /api/v1/lab/runs": () =>
        json([{ id: RUN_ID, experiment: "idempotency", status: "running", params: {}, summary: null, checks_passed: 0, checks_total: 0, created_at: "2026-11-18T12:00:00Z", finished_at: null }]),
    });
    const form = await screen.findByRole("form", { name: "Concurrent withdrawals parameters" });
    await waitFor(() => expect(within(form).getByRole("button", { name: "Run experiment" })).toBeDisabled());
    expect(within(form).getByText(/Runs never overlap/)).toBeInTheDocument();
  });

  it("lists run history compactly and opens a run from it", async () => {
    const { user, router } = setup({
      "GET /api/v1/lab/runs": () =>
        json([
          { id: RUN_ID, experiment: "idempotency", status: "passed", params: {}, summary: "x", checks_passed: 12, checks_total: 12, created_at: new Date().toISOString(), finished_at: null },
        ]),
      "GET /api/v1/lab/runs/:id": () => json(passedConcurrencyRun()),
      "GET /api/v1/lab/runs/:id/events": () => eventsPage([], { status: "passed", event_count: 0 }),
    });
    const history = await screen.findByRole("list", { name: "Recent runs" });
    const row = within(history).getByRole("button", { name: /Idempotency.*Passed.*just now/ });
    await user.click(row);
    expect(router.state.location.search).toContain(`run=${RUN_ID}`);
    expect(await screen.findByTestId("run-panel")).toBeInTheDocument();
  });
});

describe("experiment-specific results", () => {
  it("load balancing: shows the distribution and marks single-replica checks as not applicable", async () => {
    const run = labRun({
      experiment: "load_balancing",
      status: "passed",
      params: { requests: 6, concurrency: 1 },
      finished_at: "2026-11-18T12:00:02Z",
      result: {
        summary: "6 reads were served by 2 replica(s): api-1 3, api-2 3 (spread 0).",
        explanation: ["NGINX round robin."],
        metrics: [{ key: "replicas", label: "Replicas seen", value: 2, tone: "info", unit: null, hint: null }],
        outcome_counts: { read_ok: 6 },
        data: { sequence: ["api-1", "api-2", "api-1", "api-2", "api-1", "api-2"], counts: { "api-1": 3, "api-2": 3 }, fair_share: 3, spread: 0, tolerance: 2, alternations: 5, latency_by_replica_ms: {} },
      },
      checks: [
        { id: "all_succeeded", label: "Every request succeeded", status: "pass", observed: "6/6 answered 200", description: null },
        { id: "even_split", label: "Traffic is evenly split", status: "skipped", observed: "needs at least two replicas", description: null },
      ],
    });
    const { user } = setup(
      {
        "GET /api/v1/lab/runs/:id": () => json(run),
        "GET /api/v1/lab/runs/:id/events": () =>
          eventsPage([requestEvent(1, observation({ method: "GET", path: "/api/v1/accounts/x", status: 200, outcome: "read_ok" }))], { status: "passed", event_count: 1 }),
      },
      `/experiments?experiment=load_balancing&run=${RUN_ID}`,
    );
    // The summary shows the per-replica bars right away.
    const summaryBars = await screen.findByRole("list", { name: "Requests per replica" });
    expect(within(summaryBars).getByText("3 · 50%", { selector: "[data-replica='api-1'] *" })).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: /Checks/ }));
    expect(screen.getByTestId("invariant-summary")).toHaveTextContent("1 of 1 checks passed· 1 not applicable");
    const skipped = within(screen.getByRole("list", { name: "Experiment invariants" })).getByText("Traffic is evenly split").closest("li")!;
    expect(within(skipped).getByText("Not applicable")).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: /Technical details/ }));
    const result = screen.getByTestId("result-load-balancing");
    expect(within(result).getAllByRole("listitem", { name: /Request \d: api-/ })).toHaveLength(6);
  });

  it("rate limiting: shows admitted vs 429, Retry-After and the per-replica counterfactual", async () => {
    const run = labRun({
      experiment: "rate_limiting",
      status: "passed",
      params: { burst_requests: 6, refill_wait_seconds: 2, probe_requests: 4 },
      finished_at: "2026-11-18T12:00:05Z",
      result: {
        summary: "Burst of 6: 4 admitted, 2 rejected with 429.",
        explanation: ["Token bucket."],
        metrics: [
          { key: "burst", label: "Burst size", value: 6, tone: "neutral", unit: null, hint: null },
          { key: "allowed", label: "Admitted (201)", value: 4, tone: "success", unit: null, hint: null },
          { key: "rejected", label: "Rejected (429)", value: 2, tone: "warning", unit: null, hint: null },
        ],
        outcome_counts: { committed: 4, rate_limited: 2 },
        data: {
          policy: { name: "write", capacity: 4, tokens_per_second: 2 },
          limiter_backend: "RedisTokenBucketLimiter",
          burst: { allowed: 4, rejected: 2, elapsed_ms: 120, admitted_sequence: [true, true, false, true, true, false], by_replica: { "api-1": { admitted: 2, rejected: 1 }, "api-2": { admitted: 2, rejected: 1 } } },
          probe: { requests: 4, wait_seconds: 2, allowed: 4, expected: 4, allowed_range: [4, 5] },
          retry_after_values: [1],
          counterfactual_per_replica: 6,
        },
      },
      checks: [{ id: "global_quota", label: "Global quota respected during the burst", status: "pass", observed: "4 admitted", description: null }],
    });
    const { user } = setup(
      {
        "GET /api/v1/lab/runs/:id": () => json(run),
        "GET /api/v1/lab/runs/:id/events": () =>
          eventsPage(
            [
              requestEvent(1, observation({ outcome: "committed" })),
              requestEvent(2, observation({ index: 1, status: 429, code: "RATE_LIMITED", outcome: "rate_limited", retry_after_s: 1, replica: "api-2" })),
            ],
            { status: "passed", event_count: 2 },
          ),
      },
      `/experiments?experiment=rate_limiting&run=${RUN_ID}`,
    );
    expect(await screen.findByRole("img", { name: "Burst: 4 admitted, 2 rejected (429)" })).toBeInTheDocument();
    const flow = screen.getByRole("figure", { name: "Request flow" });
    await waitFor(() => expect(flow.querySelector('[data-node="redis"]')!.textContent).toContain("1 blocked"));

    await user.click(screen.getByRole("tab", { name: /Technical details/ }));
    const result = screen.getByTestId("result-rate-limit");
    const strip = within(result).getByRole("list", { name: "Burst requests: admitted or rejected" });
    expect(within(strip).getAllByLabelText(/rejected 429/)).toHaveLength(2);
    expect(within(strip).getAllByLabelText(/admitted$/)).toHaveLength(4);
    expect(within(result).getByText("Per-replica counters (counterfactual)")).toBeInTheDocument();
    expect(within(result).getByText("RedisTokenBucketLimiter")).toBeInTheDocument();
    expect(within(result).getByText(/after waiting 2 s/)).toBeInTheDocument();
  });

  it("an error run explains why it did not complete", async () => {
    setup(
      {
        "GET /api/v1/lab/runs/:id": () =>
          json(labRun({ status: "error", error: "The run exceeded its 120 s limit and was stopped.", finished_at: "2026-11-18T12:02:00Z", progress: { phase: "error", message: "timeout", completed: 3, total: 100 } })),
        "GET /api/v1/lab/runs/:id/events": () => eventsPage([], { status: "error", event_count: 0 }),
      },
      `/experiments?run=${RUN_ID}`,
    );
    expect(await screen.findByText("The run did not complete")).toBeInTheDocument();
    expect(screen.getByText("The run exceeded its 120 s limit and was stopped.")).toBeInTheDocument();
    expect(screen.getByTestId("run-status")).toHaveAttribute("data-status", "error");
  });
});
