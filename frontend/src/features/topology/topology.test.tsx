import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { HEALTHY_STACK, component, snapshot } from "@/test/observabilityFixtures";
import { apiError, json, mockApi, readyOk } from "@/test/mockApi";
import { renderApp } from "@/test/render";

function setup(topology: Parameters<typeof mockApi>[0][string]) {
  const api = mockApi({ "GET /health/ready": readyOk, "GET /api/v1/observability/topology": topology });
  return { api, ...renderApp("/topology") };
}

function node(id: string) {
  return document.querySelector(`[data-component="${id}"]`) as HTMLElement;
}

afterEach(() => vi.useRealTimers());

describe("live system topology", () => {
  it("shows every component with its reported status and a verdict", async () => {
    setup(() => json(snapshot(HEALTHY_STACK)));
    expect(await screen.findByText("All 9 components healthy")).toBeInTheDocument();
    expect(node("api-2")).toHaveAttribute("data-status", "healthy");
    expect(node("anomaly-detector")).toHaveAttribute("data-status", "not_configured");
    // The browser node is added client-side and is genuinely observed (this page runs).
    expect(node("browser")).toHaveAttribute("data-status", "healthy");
  });

  it("reports failures exactly as the backend observed them", async () => {
    const failing = HEALTHY_STACK.map((c) =>
      c.id === "redis"
        ? { ...c, status: "unhealthy" as const, message: "Not reachable", last_error: "TimeoutError: connect" }
        : c.id === "event-consumer"
          ? { ...c, status: "stale" as const, message: "Last heartbeat 40 s ago" }
          : c,
    );
    const { user } = setup(() => json(snapshot(failing)));
    expect(await screen.findByText("2 of 9 components need attention")).toBeInTheDocument();
    expect(node("redis")).toHaveAttribute("data-status", "unhealthy");
    expect(node("event-consumer")).toHaveAttribute("data-status", "stale");

    await user.click(node("redis"));
    const details = await screen.findByTestId("component-details");
    expect(within(details).getByText("Not reachable")).toBeInTheDocument();
    expect(within(details).getByText("TimeoutError: connect")).toBeInTheDocument();
    expect(within(details).getByText("Probed")).toBeInTheDocument();
  });

  it("never shows an old snapshot as current when polling fails", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    const { api } = setup([() => json(snapshot(HEALTHY_STACK)), () => apiError(502, "HTTP_502", "Bad gateway")]);
    expect(await screen.findByText("All 9 components healthy")).toBeInTheDocument();
    vi.setSystemTime(Date.now() + 20_000);
    expect(await screen.findByText("Live status is out of date", {}, { timeout: 8_000 })).toBeInTheDocument();
    expect(node("api-1")).toHaveAttribute("data-status", "stale");
    expect(node("anomaly-detector")).toHaveAttribute("data-status", "not_configured");
    expect(api.calls("GET", "/api/v1/observability/topology").length).toBeGreaterThanOrEqual(1);
  }, 12_000);

  it("explains a disabled backend instead of inventing statuses", async () => {
    setup(() => apiError(404, "NOT_FOUND", "Not Found"));
    expect(await screen.findByText("Live topology is disabled on this backend")).toBeInTheDocument();
    expect(document.querySelector("[data-component]")).toBeNull();
  });

  it("degraded worker with unknown broker is not counted as healthy", async () => {
    const comps = [component({ id: "outbox-publisher", kind: "worker", status: "degraded" }), component({ id: "rabbitmq", kind: "broker", status: "unknown" })];
    setup(() => json(snapshot(comps)));
    await waitFor(() => expect(screen.getByTestId("topology-verdict")).toHaveTextContent("2 of 3 components need attention"));
  });
});
