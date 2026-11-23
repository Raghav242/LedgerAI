import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ResponseMeta } from "@/api/client";
import { EVENT_ID, TRANSFER_ID, eventSummary, lifecycle, pipelineSummary } from "@/test/observabilityFixtures";
import { json, mockApi, readyOk } from "@/test/mockApi";
import { renderApp } from "@/test/render";
import type { TraceView } from "@/types/api";
import { buildLifecycle } from "./lifecycleModel";

const META: ResponseMeta = {
  status: 201,
  requestId: "c418d6af4a41e4b0",
  servedBy: "api-2",
  traceId: null,
  rateLimit: { policy: "write", limit: 20, remaining: 17 },
  retryAfterSeconds: null,
  idempotentReplayed: false,
  durationMs: 48,
};

const byId = (stages: ReturnType<typeof buildLifecycle>) => Object.fromEntries(stages.map((s) => [s.id, s]));

describe("lifecycle model", () => {
  it("uses response facts for the synchronous half and persisted state for the asynchronous half", () => {
    const s = byId(buildLifecycle({ sync: { meta: META, status: 201, replayed: false }, lifecycle: lifecycle() }));
    expect(s.replica!.evidence).toBe("observed");
    expect(s.replica!.detail).toContain("api-2");
    expect(s.rate_limit!.source).toBe("X-RateLimit headers");
    expect(s.response!.durationMs).toBe(48);
    expect(s.publish!.evidence).toBe("observed");
    expect(s.publish!.durationMs).toBe(300);
    expect(s["consume:ledger-audit"]!.async).toBe(true);
  });

  it("without response facts or a trace, never claims observation or timing it does not have", () => {
    const s = byId(buildLifecycle({ sync: null, lifecycle: lifecycle(), trace: null }));
    expect(s.replica!.evidence).toBe("unknown");
    expect(s.rate_limit!.evidence).toBe("derived");
    expect(s.rate_limit!.durationMs).toBeNull();
    expect(s.idempotency!.evidence).toBe("derived");
    expect(s.response!.evidence).toBe("derived");
    expect(s.database!.durationMs).toBeNull();
  });

  it("takes timings from trace spans when the trace exists", () => {
    const trace: TraceView = {
      trace_id: "a".repeat(32),
      available: true,
      services: ["ledgerflow-api"],
      truncated: false,
      spans: [
        { span_id: "1", parent_id: null, name: "POST /api/v1/transfers", service: "ledgerflow-api", instance: "api-1", start_offset_ms: 0, duration_ms: 40, error: false, attributes: {} },
        { span_id: "2", parent_id: "1", name: "rate_limit.check", service: "ledgerflow-api", instance: "api-1", start_offset_ms: 1, duration_ms: 0.7, error: false, attributes: { "ledgerflow.rate_limit.decision": "allowed" } },
        { span_id: "3", parent_id: "1", name: "transfer.create", service: "ledgerflow-api", instance: "api-1", start_offset_ms: 3, duration_ms: 30, error: false, attributes: {} },
      ],
    };
    const s = byId(buildLifecycle({ sync: null, lifecycle: lifecycle(), trace }));
    expect(s.replica!.detail).toContain("api-1");
    expect(s.rate_limit!.durationMs).toBe(0.7);
    expect(s.database!.durationMs).toBe(30);
  });

  it("shows a failed publication as failed-and-retrying, and pending consumers as pending", () => {
    const pending = lifecycle({
      stages: [
        lifecycle().stages[0]!,
        lifecycle().stages[1]!,
        { id: "publish", label: "Published to RabbitMQ", state: "failed", at: null, detail: "Attempt 2 failed (broker down).", duration_ms: null },
        { id: "consume:ledger-audit", label: "Consumed by ledger-audit", state: "pending", at: null, detail: "Waits until the event is published.", duration_ms: null },
      ],
    });
    const s = byId(buildLifecycle({ sync: null, lifecycle: pending }));
    expect(s.publish!.evidence).toBe("failed");
    expect(s["consume:ledger-audit"]!.evidence).toBe("pending");
  });
});

describe("event pipeline page", () => {
  it("leads with the verdict and lets you follow one event end to end", async () => {
    mockApi({
      "GET /health/ready": readyOk,
      "GET /api/v1/observability/events/summary": () => json(pipelineSummary()),
      "GET /api/v1/observability/events": () => json({ items: [eventSummary()], next_cursor: null }),
      [`GET /api/v1/observability/transfers/${TRANSFER_ID}/lifecycle`]: () => json(lifecycle()),
    });
    const { user } = renderApp("/events");
    expect(await screen.findByText("Events are flowing")).toBeInTheDocument();
    const row = await screen.findByRole("button", { name: "transfer.completed" });
    await user.click(row);
    const stages = await screen.findByTestId("lifecycle");
    expect(within(stages).getByText("Consumed by ledger-audit")).toBeInTheDocument();
    expect(document.querySelector('[data-stage="publish"]')).toHaveAttribute("data-evidence", "observed");
    expect(screen.getAllByText(EVENT_ID.slice(0, 8), { exact: false }).length).toBeGreaterThan(0);
    expect(screen.getByText(/tracing was off/)).toBeInTheDocument();
  });

  it("warns when publications are failing, naming the error", async () => {
    mockApi({
      "GET /health/ready": readyOk,
      "GET /api/v1/observability/events/summary": () =>
        json(pipelineSummary({ outbox_pending: 3, awaiting_retry: 3, publisher_last_error: "broker unavailable: AMQPConnectionError" })),
      "GET /api/v1/observability/events": () => json({ items: [eventSummary({ status: "pending", last_error: "broker down", published_at: null, consumers: [] })], next_cursor: null }),
    });
    renderApp("/events");
    expect(await screen.findByText("3 event(s) failed to publish and will be retried")).toBeInTheDocument();
    expect(screen.getByText("broker unavailable: AMQPConnectionError")).toBeInTheDocument();
    expect(within(await screen.findByRole("table")).getByText("Retrying")).toBeInTheDocument();
  });
});
