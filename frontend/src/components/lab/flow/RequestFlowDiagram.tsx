import {
  ArrowDown,
  Ban,
  Check,
  CircleDashed,
  Cog,
  CornerDownLeft,
  Database,
  Gauge,
  Inbox,
  ListOrdered,
  Loader2,
  Minus,
  Monitor,
  Repeat,
  Server,
  Split,
  X,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useId, useState, type ReactNode } from "react";
import { Disclosure } from "@/components/common/Disclosure";
import { cn } from "@/lib/utils";
import {
  ASYNC_ORDER,
  DISPLAY_MEMBERS,
  DISPLAY_ORDER,
  NODE_META,
  type AsyncNodeId,
  type DisplayNodeId,
  type Evidence,
  type FlowModel,
  type FlowNode,
  type FlowState,
} from "./flowModel";

type NodeId = DisplayNodeId | AsyncNodeId;

const ICONS: Record<NodeId, LucideIcon> = {
  client: Monitor,
  nginx: Split,
  replica: Server,
  redis: Gauge,
  database: Database,
  response: CornerDownLeft,
  outbox: Inbox,
  rabbitmq: ListOrdered,
  worker: Cog,
};

/** What each component is for, in one sentence (shown in the details panel). */
const COMPONENT_ROLE: Record<NodeId, string> = {
  client: "Sends the HTTP request: your browser, or the lab runner for experiments.",
  nginx: "The only public entry point. Load-balances requests across replicas and stamps X-Request-ID.",
  replica: "A stateless FastAPI process. Validates the request and runs the business logic.",
  redis: "Holds the shared token buckets. Every replica asks it before doing any work.",
  database: "System of record: claims the Idempotency-Key, locks rows, updates balances and posts the ledger in one transaction.",
  response: "The HTTP answer: status, body and headers such as X-Served-By and Idempotent-Replayed.",
  outbox: "An event row written in the same COMMIT as the money movement.",
  rabbitmq: "Durable queue the outbox publisher sends committed events to.",
  worker: "Consumes events and applies each one exactly once (deduplicated by event id).",
};

const STAGE_LABEL: Record<string, string> = {
  idempotency: "Idempotency key",
  postgres: "Row locks & balances",
  ledger: "Ledger postings",
};

const STATE_LABEL: Record<FlowState, string> = {
  idle: "Idle",
  pending: "Pending",
  active: "Active",
  success: "Passed",
  blocked: "Blocked",
  failed: "Failed",
  skipped: "Skipped",
  replayed: "Replayed",
};

const EVIDENCE_LABEL: Record<Evidence, { short: string; long: string }> = {
  observed: { short: "Observed", long: "Reported directly by the response (status, error code, headers or body)." },
  inferred: { short: "Inferred", long: "Deduced from the response and the server's documented order of operations; not traced." },
  pending: { short: "Not observed", long: "Expected to happen after the commit. The response does not report it." },
  unknown: { short: "Unknown", long: "No evidence either way, for example because no response arrived." },
  not_applicable: { short: "Not applicable", long: "The request never reached this component." },
  illustration: { short: "Illustration", long: "A static diagram of documented behaviour, not a real request." },
};

const STATE_STYLE: Record<FlowState, { card: string; text: string; glyph: ReactNode }> = {
  idle: { card: "border-dashed", text: "text-muted-foreground", glyph: <CircleDashed /> },
  pending: { card: "border-dashed border-info/40", text: "text-info", glyph: <CircleDashed /> },
  active: { card: "border-info bg-info/10", text: "text-info", glyph: <Loader2 className="motion-safe:animate-spin" /> },
  success: { card: "border-success/40 bg-success/5", text: "text-success", glyph: <Check /> },
  blocked: { card: "border-warning/50 bg-warning/10", text: "text-warning", glyph: <Ban /> },
  failed: { card: "border-destructive/50 bg-destructive/10", text: "text-destructive", glyph: <X /> },
  skipped: { card: "border-dashed opacity-60", text: "text-muted-foreground", glyph: <Minus /> },
  replayed: { card: "border-info/50 bg-info/10", text: "text-info", glyph: <Repeat /> },
};

const STAT_TONE = {
  success: "text-success",
  warning: "text-warning",
  destructive: "text-destructive",
  info: "text-info",
  muted: "text-muted-foreground",
} as const;

function reached(state: FlowState): boolean {
  return state !== "idle" && state !== "skipped" && state !== "pending";
}

function motionAllowed(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function" && !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/**
 * Reveals the path one component at a time, once, then rests on the final states. Changing
 * `playKey` replays it. Without motion (reduced-motion users, tests) the final state shows at once.
 */
function usePlayback(steps: number, playKey: string | number | undefined, enabled: boolean): number {
  const [state, setState] = useState({ key: playKey, shown: enabled ? 0 : steps });
  let shown = state.shown;
  if (state.key !== playKey) {
    shown = enabled ? 0 : steps;
    setState({ key: playKey, shown });
  }
  useEffect(() => {
    if (!enabled || shown >= steps) return;
    const timer = setTimeout(() => setState((s) => ({ ...s, shown: s.shown + 1 })), 160);
    return () => clearTimeout(timer);
  }, [enabled, shown, steps]);
  return enabled ? shown : steps;
}

export interface RequestFlowDiagramProps {
  model: FlowModel;
  /** Change to replay the travel animation (e.g. a new attempt or a new selected request). */
  playKey?: string | number;
  animate?: boolean;
  /** Offer the asynchronous event path (outbox → RabbitMQ → consumer); collapsed by default. */
  showAsync?: boolean;
  /** "aggregate": many requests folded together (counts on nodes). */
  mode?: "single" | "aggregate";
  /** Extra text per component in the details panel (System Topology). */
  componentInfo?: Partial<Record<NodeId, ReactNode>>;
  className?: string;
  label?: string;
}

/**
 * The Request Flow Visualizer. Six compact components in request order; click one for what
 * happened there, how we know (observed vs inferred) and the technical detail.
 */
export function RequestFlowDiagram({
  model,
  playKey,
  animate = true,
  showAsync = false,
  mode = "single",
  componentInfo,
  className,
  label = "Request flow",
}: RequestFlowDiagramProps) {
  const playing = animate && motionAllowed();
  const shown = usePlayback(DISPLAY_ORDER.length, playKey, playing);
  const done = shown >= DISPLAY_ORDER.length;
  const [selected, setSelected] = useState<NodeId | null>(null);
  const panelId = useId();

  const displayed = (index: number, node: FlowNode): FlowNode => {
    if (index < shown) return node;
    if (index === shown && reached(node.state)) return { state: "active" };
    return { state: "idle" };
  };

  const select = (id: NodeId) => setSelected((current) => (current === id ? null : id));
  const asyncRelevant = showAsync && ASYNC_ORDER.some((id) => model.async[id].state !== "skipped");

  return (
    <figure aria-label={label} data-testid="request-flow" className={cn("space-y-3", className)}>
      <ol className="flex flex-col items-stretch md:flex-row md:flex-wrap md:items-center md:gap-y-3" aria-label="Components in request order">
        {DISPLAY_ORDER.map((id, index) => {
          const node = displayed(index, model.display[id]);
          const prev = index > 0 ? displayed(index - 1, model.display[DISPLAY_ORDER[index - 1]!]) : null;
          return (
            // Nodes share the row; they only wrap when a column is narrower than six minimum-width nodes.
            <li key={id} className="flex flex-col items-stretch md:flex-1 md:flex-row md:items-center">
              {prev && <Connector from={prev.state} to={node.state} live={playing && !done && index === shown} />}
              <NodeButton
                id={id}
                node={node}
                metric={done ? nodeMetric(id, model, mode) : undefined}
                selected={selected === id}
                controls={panelId}
                onClick={() => select(id)}
                fill
              />
            </li>
          );
        })}
      </ol>

      <figcaption className="text-sm text-muted-foreground" aria-live="polite">
        {done ? model.caption : "Following the request…"}
      </figcaption>

      {selected && done && (
        <NodeDetails id={selected} panelId={panelId} model={model} mode={mode} extra={componentInfo?.[selected]} onClose={() => setSelected(null)} />
      )}

      {asyncRelevant && (
        <Disclosure
          title={
            <span>
              After commit <span className="font-normal text-muted-foreground">· Outbox → RabbitMQ → Consumer</span>
            </span>
          }
          meta={EVIDENCE_LABEL[model.evidence.rabbitmq].short}
        >
          <ol className="flex flex-col items-stretch md:flex-row md:flex-wrap md:items-center" aria-label="After-commit event path">
            {ASYNC_ORDER.map((id, index) => (
              <li key={id} className="flex flex-col items-stretch md:flex-row md:items-center">
                {index > 0 && <Connector from={model.async[ASYNC_ORDER[index - 1]!].state} to={model.async[id].state} live={false} />}
                <NodeButton
                  id={id}
                  node={model.async[id]}
                  metric={EVIDENCE_LABEL[model.evidence[id]].short}
                  selected={selected === id}
                  controls={panelId}
                  onClick={() => select(id)}
                />
              </li>
            ))}
          </ol>
          <p className="mt-2 text-xs text-muted-foreground">
            These steps run asynchronously after the transaction commits. The response does not report them, so they are never shown as observed.
          </p>
        </Disclosure>
      )}

      <EvidenceSummary model={model} />
    </figure>
  );
}

function nodeMetric(id: NodeId, model: FlowModel, mode: "single" | "aggregate"): ReactNode {
  const node = id in model.display ? model.display[id as DisplayNodeId] : model.async[id as AsyncNodeId];
  if (mode === "aggregate") {
    if (id === "replica") {
      const used = model.replicas.filter((r) => r.count);
      return used.length ? `${used.length} replica${used.length === 1 ? "" : "s"}` : STATE_LABEL[node.state];
    }
    if (id === "client") return node.detail;
    if (node.stats?.length) {
      return (
        <span className="flex flex-wrap gap-x-1.5">
          {node.stats.slice(0, 2).map((s) => (
            <span key={s.label} className={STAT_TONE[s.tone]}>
              {s.value} {s.label}
            </span>
          ))}
        </span>
      );
    }
    return STATE_LABEL[node.state];
  }
  if (id === "replica") {
    const served = model.replicas.find((r) => r.state !== "idle");
    return served && served.name !== "replica" ? served.name : STATE_LABEL[node.state];
  }
  if (id === "response" && model.status) return `${model.status} · ${STATE_LABEL[node.state]}`;
  return STATE_LABEL[node.state];
}

function NodeButton({
  id,
  node,
  metric,
  selected,
  controls,
  onClick,
  fill,
}: {
  id: NodeId;
  node: FlowNode;
  metric?: ReactNode;
  selected: boolean;
  controls: string;
  onClick: () => void;
  /** Share the row's width (main lane) instead of a fixed width (after-commit lane). */
  fill?: boolean;
}) {
  const Icon = ICONS[id];
  const meta = NODE_META[id];
  const style = STATE_STYLE[node.state];
  return (
    <button
      type="button"
      data-node={id}
      data-state={node.state}
      aria-pressed={selected}
      aria-controls={controls}
      aria-label={`${meta.label}: ${STATE_LABEL[node.state]}. Show details`}
      onClick={onClick}
      className={cn(
        "flex min-w-0 items-center gap-2 rounded-lg border px-2.5 py-2 text-left transition-colors outline-none hover:border-foreground/30 focus-visible:ring-[3px] focus-visible:ring-ring/50 motion-reduce:transition-none md:flex-col md:items-start md:gap-1",
        fill ? "md:min-w-[100px] md:flex-1 md:px-2" : "md:w-[124px]",
        style.card,
        selected && "ring-2 ring-primary/60",
      )}
    >
      <span className="flex min-w-0 items-center gap-1.5 text-xs font-semibold md:w-full">
        <Icon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
        <span className="truncate" title={meta.label}>
          {meta.label}
        </span>
      </span>
      <span className={cn("ml-auto flex min-w-0 items-center gap-1 text-[11px] md:ml-0 md:w-full", style.text)}>
        <span className="shrink-0 [&>svg]:size-3" aria-hidden>
          {style.glyph}
        </span>
        <span className="truncate">{metric ?? STATE_LABEL[node.state]}</span>
      </span>
    </button>
  );
}

function Pill({ children, className, ...rest }: { children: ReactNode; className?: string } & Record<`data-${string}`, string>) {
  return (
    <span className={cn("inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs font-medium", className)} {...rest}>
      {children}
    </span>
  );
}

function NodeDetails({
  id,
  panelId,
  model,
  mode,
  extra,
  onClose,
}: {
  id: NodeId;
  panelId: string;
  model: FlowModel;
  mode: "single" | "aggregate";
  extra?: ReactNode;
  onClose: () => void;
}) {
  const isAsync = !(id in model.display);
  const node = isAsync ? model.async[id as AsyncNodeId] : model.display[id as DisplayNodeId];
  const evidence = EVIDENCE_LABEL[model.evidence[id]];
  const style = STATE_STYLE[node.state];
  const Icon = ICONS[id];
  return (
    <section id={panelId} aria-label={`${NODE_META[id].label} details`} className="space-y-3 rounded-lg border bg-muted/20 p-4" data-testid="flow-node-details">
      <div className="flex items-start gap-3">
        <Icon className="mt-0.5 size-4 text-muted-foreground" aria-hidden />
        <div className="min-w-0 flex-1">
          <h4 className="text-sm font-semibold">{NODE_META[id].label}</h4>
          <p className="text-xs text-muted-foreground">{COMPONENT_ROLE[id]}</p>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close details"
          className="rounded-md p-1 text-muted-foreground outline-none hover:bg-accent focus-visible:ring-[3px] focus-visible:ring-ring/50"
        >
          <X className="size-4" />
        </button>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Pill className={style.text}>
          <span className="[&>svg]:size-3" aria-hidden>
            {style.glyph}
          </span>
          {STATE_LABEL[node.state]}
        </Pill>
        <Pill className="text-muted-foreground" data-evidence={model.evidence[id]}>
          {evidence.short}
        </Pill>
        <span className="text-xs text-muted-foreground">{evidence.long}</span>
      </div>

      {node.detail && mode === "single" && <p className="text-sm">{node.detail}</p>}

      {node.stats && node.stats.length > 0 && (
        <ul className="flex flex-wrap gap-x-4 gap-y-1 text-sm" aria-label="Requests at this component">
          {node.stats.map((s) => (
            <li key={s.label} className={cn("font-mono tabular", STAT_TONE[s.tone])}>
              {s.value} {s.label}
            </li>
          ))}
        </ul>
      )}

      {id === "database" && (
        <ul className="divide-y rounded-md border text-sm" aria-label="PostgreSQL stages">
          {DISPLAY_MEMBERS.database.map((member) => {
            const stage = model.nodes[member];
            const stageStyle = STATE_STYLE[stage.state];
            return (
              <li key={member} data-stage={member} data-state={stage.state} className="flex flex-wrap items-center gap-2 px-3 py-2">
                <span className={cn("[&>svg]:size-3.5", stageStyle.text)} aria-hidden>
                  {stageStyle.glyph}
                </span>
                <span className="font-medium">{STAGE_LABEL[member]}</span>
                <span className="text-xs text-muted-foreground">
                  {STATE_LABEL[stage.state]}
                  {mode === "single" && stage.detail ? ` · ${stage.detail}` : ""}
                  {mode === "aggregate" && stage.stats?.length ? ` · ${stage.stats.map((s) => `${s.value} ${s.label}`).join(", ")}` : ""}
                </span>
              </li>
            );
          })}
        </ul>
      )}

      {id === "replica" && (
        <ul className="flex flex-wrap gap-2" aria-label="Replicas">
          {model.replicas.map((r) => (
            <li
              key={r.name}
              data-replica={r.name}
              data-state={r.state}
              className={cn("rounded-md border px-2 py-1 font-mono text-xs", STATE_STYLE[r.state].card, r.state === "idle" && "text-muted-foreground")}
            >
              {r.name}
              {r.count !== undefined && <span className="ml-1.5 tabular">{r.count}</span>}
            </li>
          ))}
        </ul>
      )}

      {extra && <div className="text-sm text-muted-foreground">{extra}</div>}
    </section>
  );
}

function Connector({ from, to, live }: { from: FlowState; to: FlowState; live: boolean }) {
  const travelled = reached(from) && from !== "blocked" && from !== "failed" && (reached(to) || to === "active");
  const color = !travelled
    ? "text-border"
    : to === "blocked"
      ? "text-warning"
      : to === "failed"
        ? "text-destructive"
        : to === "replayed"
          ? "text-info"
          : "text-success/70";
  return (
    <div className={cn("flex items-center justify-center", color)} aria-hidden data-edge={travelled ? "travelled" : "idle"}>
      <div className="flex flex-col items-center py-0.5 md:hidden">
        <div className={cn("h-3 w-0.5", live ? "flow-live-y" : "bg-current")} />
        <ArrowDown className="-mt-1 size-3" />
      </div>
      <div className="hidden items-center md:flex">
        <div className={cn("h-0.5 w-2", live ? "flow-live-x" : "bg-current")} />
        <div className="size-0 border-y-[4px] border-l-[6px] border-y-transparent border-l-current" />
      </div>
    </div>
  );
}

/** One line: which components we actually saw evidence for, and which are inferred. */
function EvidenceSummary({ model }: { model: FlowModel }) {
  const groups = new Map<Evidence, string[]>();
  for (const id of DISPLAY_ORDER) {
    const ev = model.evidence[id];
    groups.set(ev, [...(groups.get(ev) ?? []), NODE_META[id].label]);
  }
  if (groups.has("illustration")) {
    return <p className="text-xs text-muted-foreground">Illustration of documented behaviour, not a traced request. Select a component for details.</p>;
  }
  const order: Evidence[] = ["observed", "inferred", "unknown"];
  const parts = order.filter((e) => groups.has(e)).map((e) => `${EVIDENCE_LABEL[e].short}: ${groups.get(e)!.join(", ")}`);
  if (!parts.length) return null;
  return (
    <p className="text-xs text-muted-foreground" data-testid="flow-evidence">
      {parts.join(" · ")} · Select a component for details.
    </p>
  );
}
