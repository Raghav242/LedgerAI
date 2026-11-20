import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { aggregateFlow, asIllustration, flowForOutcome, idleFlow, outcomeForCode, type FlowNodeId, type FlowState } from "./flowModel";
import { ReplicaDistribution } from "./ReplicaDistribution";
import { RequestFlowDiagram } from "./RequestFlowDiagram";

function states(model: ReturnType<typeof flowForOutcome>): Record<FlowNodeId, FlowState> {
  return Object.fromEntries(Object.entries(model.nodes).map(([id, n]) => [id, n.state])) as Record<FlowNodeId, FlowState>;
}

describe("flow model: one request", () => {
  it("a committed transfer passes every component and writes the outbox event", () => {
    const model = flowForOutcome("committed", { replica: "api-2", replicas: ["api-1", "api-2"], status: 201 });
    expect(states(model)).toEqual({
      client: "success",
      nginx: "success",
      replica: "success",
      redis: "success",
      idempotency: "success",
      postgres: "success",
      ledger: "success",
      response: "success",
    });
    expect(model.replicas).toEqual([
      { name: "api-1", state: "idle" },
      { name: "api-2", state: "success" },
    ]);
    expect(model.async.outbox.state).toBe("success");
    expect(model.async.rabbitmq.state).toBe("pending");
  });

  it("an idempotent replay stops at the idempotency claim and touches no balance", () => {
    const s = states(flowForOutcome("replayed", { replica: "api-1" }));
    expect(s.idempotency).toBe("replayed");
    expect(s.response).toBe("replayed");
    expect(s.postgres).toBe("skipped");
    expect(s.ledger).toBe("skipped");
  });

  it("a rate-limited request is blocked at Redis before any database work", () => {
    const model = flowForOutcome("rate_limited", { replica: "api-1", status: 429 });
    const s = states(model);
    expect(s.redis).toBe("blocked");
    expect(s.response).toBe("blocked");
    for (const id of ["idempotency", "postgres", "ledger"] as const) expect(s[id]).toBe("skipped");
    expect(model.async.outbox.state).toBe("skipped");
  });

  it("insufficient funds is blocked at PostgreSQL after locking, with the key claimed", () => {
    const s = states(flowForOutcome("insufficient_funds"));
    expect(s.idempotency).toBe("success");
    expect(s.postgres).toBe("blocked");
    expect(s.ledger).toBe("skipped");
  });

  it("no response leaves everything after NGINX unknown, not skipped", () => {
    const model = flowForOutcome("transport_error");
    expect(model.nodes.nginx.state).toBe("failed");
    expect(model.nodes.postgres.state).toBe("idle");
    expect(model.replicas.every((r) => r.state === "idle")).toBe(true);
  });

  it.each([
    [201, "", false, "committed"],
    [201, "", true, "replayed"],
    [429, "RATE_LIMITED", false, "rate_limited"],
    [409, "INSUFFICIENT_FUNDS", false, "insufficient_funds"],
    [409, "IDEMPOTENCY_REQUEST_IN_PROGRESS", false, "in_progress"],
    [503, "RATE_LIMITER_UNAVAILABLE", false, "limiter_unavailable"],
    [503, "CONCURRENCY_CONFLICT", false, "conflict"],
    [422, "IDEMPOTENCY_KEY_REUSED", false, "key_reused"],
    [422, "VALIDATION_ERROR", false, "invalid"],
    [404, "ACCOUNT_NOT_FOUND", false, "rejected"],
    [502, "HTTP_502", false, "server_error"],
    [null, "NETWORK_ERROR", false, "transport_error"],
  ] as const)("maps %s %s (replayed=%s) to %s", (status, code, replayed, expected) => {
    expect(outcomeForCode(status, code, replayed)).toBe(expected);
  });
});

describe("flow model: many requests", () => {
  it("folds outcomes into per-node counts and per-replica totals", () => {
    const requests = [
      ...Array.from({ length: 10 }, (_, i) => ({ outcome: "committed" as const, replica: i % 2 ? "api-2" : "api-1" })),
      ...Array.from({ length: 90 }, (_, i) => ({ outcome: "insufficient_funds" as const, replica: i % 2 ? "api-2" : "api-1" })),
    ];
    const model = aggregateFlow(requests, "caption");
    expect(model.nodes.client.detail).toBe("100 requests sent");
    expect(model.display.database.state).toBe("blocked");
    expect(model.nodes.postgres.state).toBe("blocked"); // the majority were blocked there
    expect(model.nodes.postgres.stats).toEqual(
      expect.arrayContaining([
        { label: "passed", value: 10, tone: "success" },
        { label: "blocked", value: 90, tone: "warning" },
      ]),
    );
    expect(model.nodes.ledger.stats).toEqual([{ label: "passed", value: 10, tone: "success" }]);
    expect(model.replicas).toEqual([
      { name: "api-1", state: "success", count: 50 },
      { name: "api-2", state: "success", count: 50 },
    ]);
  });
});

describe("RequestFlowDiagram", () => {
  it("renders six compact components with their states", () => {
    const model = flowForOutcome("rate_limited", { replica: "api-2", replicas: ["api-1", "api-2"], status: 429 });
    render(<RequestFlowDiagram model={model} />);
    const figure = screen.getByRole("figure", { name: "Request flow" });
    const node = (id: string) => figure.querySelector(`[data-node="${id}"]`)!;
    expect([...figure.querySelectorAll("[data-node]")].map((n) => n.getAttribute("data-node"))).toEqual([
      "client",
      "nginx",
      "replica",
      "redis",
      "database",
      "response",
    ]);
    expect(node("redis")).toHaveAttribute("data-state", "blocked");
    expect(node("redis")).toHaveAccessibleName("Redis: Blocked. Show details");
    expect(node("database")).toHaveAttribute("data-state", "skipped");
    expect(node("replica")).toHaveTextContent("api-2"); // the serving replica, on the node
    expect(node("response")).toHaveTextContent("429");
    expect(screen.getByText(/shared token bucket in Redis was empty/)).toBeInTheDocument();
    // No details until a component is selected.
    expect(screen.queryByTestId("flow-node-details")).toBeNull();
  });

  it("opens a details panel for a selected component, with what happened and how we know", async () => {
    const user = userEvent.setup();
    const model = flowForOutcome("insufficient_funds", { replica: "api-2", replicas: ["api-1", "api-2"], status: 409, rateHeaders: true });
    render(<RequestFlowDiagram model={model} />);
    const database = screen.getByRole("button", { name: "PostgreSQL: Blocked. Show details" });
    await user.click(database);
    expect(database).toHaveAttribute("aria-pressed", "true");
    const details = screen.getByRole("region", { name: "PostgreSQL details" });
    expect(within(details).getByText(/claims the Idempotency-Key, locks rows/)).toBeInTheDocument();
    // An error code is evidence of the rejection, but the lock itself is inferred from the documented order.
    expect(within(details).getByText("Inferred")).toHaveAttribute("data-evidence", "inferred");
    const stages = within(details).getByRole("list", { name: "PostgreSQL stages" });
    expect(stages.querySelector('[data-stage="idempotency"]')).toHaveAttribute("data-state", "success");
    expect(stages.querySelector('[data-stage="postgres"]')).toHaveAttribute("data-state", "blocked");
    expect(stages.querySelector('[data-stage="ledger"]')).toHaveAttribute("data-state", "skipped");

    // Replica details list every replica and highlight the one that served the request.
    await user.click(screen.getByRole("button", { name: /^API replica:/ }));
    const replicas = within(screen.getByRole("region", { name: "API replica details" })).getByRole("list", { name: "Replicas" });
    expect(replicas.querySelector('[data-replica="api-2"]')).toHaveAttribute("data-state", "success");
    expect(replicas.querySelector('[data-replica="api-1"]')).toHaveAttribute("data-state", "idle");
    expect(within(screen.getByRole("region", { name: "API replica details" })).getByText("Observed")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Close details" }));
    expect(screen.queryByTestId("flow-node-details")).toBeNull();
  });

  it("is keyboard operable", async () => {
    const user = userEvent.setup();
    render(<RequestFlowDiagram model={flowForOutcome("committed", { replica: "api-1", status: 201 })} />);
    await user.tab(); // client
    await user.tab(); // nginx
    expect(screen.getByRole("button", { name: /^NGINX:/ })).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(screen.getByRole("region", { name: "NGINX details" })).toBeInTheDocument();
    await user.keyboard(" ");
    expect(screen.queryByRole("region", { name: "NGINX details" })).toBeNull();
  });

  it("summarises observed versus inferred components", () => {
    render(<RequestFlowDiagram model={flowForOutcome("committed", { replica: "api-1", status: 201, requestId: true })} />);
    expect(screen.getByTestId("flow-evidence")).toHaveTextContent("Observed: Client, NGINX, API replica, PostgreSQL, Response · Inferred: Redis");
  });

  it("shows aggregate counts and keeps the after-commit path collapsed and never observed", async () => {
    const user = userEvent.setup();
    const model = aggregateFlow(
      [
        { outcome: "committed", replica: "api-1" },
        { outcome: "replayed", replica: "api-2" },
        { outcome: "replayed", replica: "api-1" },
      ],
      "three requests",
    );
    render(<RequestFlowDiagram model={model} mode="aggregate" showAsync />);
    const figure = screen.getByRole("figure");
    const database = figure.querySelector('[data-node="database"]') as HTMLElement;
    expect(database).toHaveAttribute("data-state", "replayed");
    expect(database).toHaveTextContent("1 passed");
    expect(database).toHaveTextContent("2 replayed");
    expect(figure.querySelector('[data-node="replica"]')).toHaveTextContent("2 replicas");

    // Collapsed by default.
    expect(figure.querySelector('[data-node="outbox"]')).toBeNull();
    await user.click(screen.getByRole("button", { name: /After commit/ }));
    const outbox = figure.querySelector('[data-node="outbox"]') as HTMLElement;
    expect(outbox).toHaveAttribute("data-state", "success"); // the one committed transfer wrote an event row
    expect(figure.querySelector('[data-node="rabbitmq"]')).toHaveTextContent("Not observed");
    await user.click(figure.querySelector('[data-node="rabbitmq"]') as HTMLElement);
    expect(within(screen.getByRole("region", { name: "RabbitMQ details" })).getByText("Not observed")).toHaveAttribute("data-evidence", "pending");
  });

  it("idle model: nothing is claimed to have happened", () => {
    render(<RequestFlowDiagram model={idleFlow()} />);
    const nodes = screen.getByRole("figure").querySelectorAll("[data-node]");
    expect(nodes.length).toBe(6);
    nodes.forEach((n) => expect(n).toHaveAttribute("data-state", "idle"));
  });

  it("illustrations are labelled as such, never as observations", async () => {
    const user = userEvent.setup();
    render(<RequestFlowDiagram model={asIllustration(flowForOutcome("committed", { replica: "api-1" }))} componentInfo={{ redis: "redis:7.4, no persistence" }} />);
    expect(screen.getByText(/Illustration of documented behaviour/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /^Redis:/ }));
    const details = screen.getByRole("region", { name: "Redis details" });
    expect(within(details).getByText("Illustration")).toBeInTheDocument();
    expect(within(details).getByText("redis:7.4, no persistence")).toBeInTheDocument();
  });
});

describe("evidence classification", () => {
  it("marks components observed only when the response proves them", () => {
    const committed = flowForOutcome("committed", { replica: "api-1", requestId: true, rateHeaders: true });
    expect(committed.evidence).toMatchObject({
      client: "observed",
      nginx: "observed",
      replica: "observed",
      redis: "observed",
      database: "observed",
      response: "observed",
      outbox: "inferred",
      rabbitmq: "pending",
      worker: "pending",
    });
    const noHeaders = flowForOutcome("committed");
    expect(noHeaders.evidence).toMatchObject({ nginx: "inferred", replica: "inferred", redis: "inferred" });
    const limited = flowForOutcome("rate_limited");
    expect(limited.evidence).toMatchObject({ redis: "observed", database: "not_applicable", outbox: "not_applicable" });
    const lost = flowForOutcome("transport_error");
    expect(lost.evidence).toMatchObject({ replica: "unknown", database: "unknown", response: "observed" });
  });
});

describe("ReplicaDistribution", () => {
  it("shows per-replica counts and the request order", () => {
    render(<ReplicaDistribution sequence={["api-1", "api-2", "api-1"]} counts={{ "api-1": 2, "api-2": 1 }} />);
    const list = screen.getByRole("list", { name: "Requests per replica" });
    expect(within(list).getByText("2 · 67%")).toBeInTheDocument();
    expect(within(list).getByText("1 · 33%")).toBeInTheDocument();
    expect(screen.getByLabelText("Request 2: api-2")).toBeInTheDocument();
  });
});
