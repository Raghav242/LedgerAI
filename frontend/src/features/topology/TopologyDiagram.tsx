import { BrainCircuit, Cog, Database, Gauge, ListOrdered, Monitor, Radar, Send, Server, Split, type LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ComponentHealth } from "@/types/api";
import { StatusDot } from "@/features/observability/health";
import { STATUS_META } from "@/features/observability/healthMeta";

/** Canvas units; nodes and edges are laid out on this grid and scaled to the container. */
const W = 1000;
const H = 470;
const NODE_W = 160;
const NODE_H = 54;
const ASYNC_Y = 278;

const ICONS: Record<string, LucideIcon> = {
  browser: Monitor,
  nginx: Split,
  api: Server,
  redis: Gauge,
  postgres: Database,
  "outbox-publisher": Send,
  rabbitmq: ListOrdered,
  "event-consumer": Cog,
  "anomaly-detector": BrainCircuit,
  jaeger: Radar,
};

/** Short names keep compact nodes readable; the full name is in the details panel. */
const SHORT_NAME: Record<string, string> = {
  nginx: "NGINX",
  "outbox-publisher": "Publisher",
  "event-consumer": "Consumer",
  "anomaly-detector": "AI worker",
  jaeger: "Jaeger",
};

const FIXED: Record<string, [number, number]> = {
  browser: [95, 130],
  nginx: [280, 130],
  redis: [690, 60],
  postgres: [690, 205],
  "outbox-publisher": [890, 205],
  rabbitmq: [890, 370],
  "event-consumer": [690, 340],
  "anomaly-detector": [690, 425],
  jaeger: [95, 400],
};

function layout(components: ComponentHealth[]): Map<string, [number, number]> {
  const positions = new Map<string, [number, number]>();
  const replicas = components.filter((c) => c.kind === "api");
  const top = 45;
  const bottom = 215;
  replicas.forEach((c, i) => {
    const y = replicas.length === 1 ? 130 : top + ((bottom - top) * i) / (replicas.length - 1);
    positions.set(c.id, [470, y]);
  });
  for (const c of components) if (FIXED[c.id]) positions.set(c.id, FIXED[c.id]!);
  return positions;
}

type Edge = { from: string; to: string; async?: boolean; curve?: boolean };

function edgesFor(components: ComponentHealth[]): Edge[] {
  const ids = new Set(components.map((c) => c.id));
  const replicas = components.filter((c) => c.kind === "api").map((c) => c.id);
  const edges: Edge[] = [{ from: "browser", to: "nginx" }];
  for (const r of replicas) edges.push({ from: "nginx", to: r }, { from: r, to: "redis" }, { from: r, to: "postgres" });
  edges.push(
    { from: "postgres", to: "outbox-publisher", async: true },
    { from: "outbox-publisher", to: "rabbitmq", async: true },
    { from: "rabbitmq", to: "event-consumer", async: true },
    { from: "event-consumer", to: "postgres", async: true },
  );
  if (ids.has("anomaly-detector")) {
    edges.push({ from: "rabbitmq", to: "anomaly-detector", async: true }, { from: "anomaly-detector", to: "postgres", async: true, curve: true });
  }
  return edges.filter((e) => ids.has(e.from) && ids.has(e.to));
}

function anchor([x, y]: [number, number], [tx, ty]: [number, number]): [number, number] {
  // Point on the node border facing the target.
  const dx = tx - x;
  const dy = ty - y;
  if (Math.abs(dx) * NODE_H > Math.abs(dy) * NODE_W) return [x + Math.sign(dx) * (NODE_W / 2), y + (dy * (NODE_W / 2)) / Math.abs(dx || 1)];
  return [x + (dx * (NODE_H / 2)) / Math.abs(dy || 1), y + Math.sign(dy) * (NODE_H / 2)];
}

function primaryMetric(c: ComponentHealth): string | null {
  if (c.status === "not_configured") return null;
  if (c.latency_ms !== null && c.latency_ms !== undefined) return `${c.latency_ms.toFixed(c.latency_ms < 10 ? 1 : 0)} ms`;
  const ready = c.metrics?.find((m) => m.key.startsWith("queue:") && m.label.startsWith("main"));
  if (ready) return `${ready.value} queued`;
  const processed = c.metrics?.find((m) => m.key === "processed" || m.key === "published");
  if (processed) return `${processed.value} ${processed.key}`;
  return null;
}

export function TopologyNode({
  component,
  selected,
  onSelect,
  style,
  className,
}: {
  component: ComponentHealth;
  selected: boolean;
  onSelect: () => void;
  style?: React.CSSProperties;
  className?: string;
}) {
  const meta = STATUS_META[component.status];
  const Icon = ICONS[component.id] ?? ICONS[component.kind] ?? Server;
  const metric = primaryMetric(component);
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      data-component={component.id}
      data-status={component.status}
      aria-label={`${component.name}: ${meta.label}`}
      style={style}
      className={cn(
        "flex items-center gap-2 rounded-lg border bg-card px-2.5 py-1.5 text-left shadow-xs outline-none transition-colors hover:bg-accent/50 focus-visible:ring-[3px] focus-visible:ring-ring/50 motion-reduce:transition-none",
        meta.card,
        selected && "ring-2 ring-primary",
        className,
      )}
    >
      <Icon className="size-4 shrink-0 text-muted-foreground" aria-hidden />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-xs font-semibold">{SHORT_NAME[component.id] ?? component.name}</span>
        <span className={cn("flex items-center gap-1 text-[11px]", meta.text)}>
          <StatusDot status={component.status} className="size-2" />
          <span className="truncate">{meta.label}</span>
          {metric && <span className="ml-auto truncate pl-1 text-muted-foreground tabular-nums">{metric}</span>}
        </span>
      </span>
    </button>
  );
}

/**
 * The architecture as a graph (md and up): compact nodes on a fixed grid, edges in SVG behind
 * them, and a dashed line separating the synchronous request path from the asynchronous event path.
 */
export function TopologyDiagram({
  components,
  selectedId,
  onSelect,
}: {
  components: ComponentHealth[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const positions = layout(components);
  const edges = edgesFor(components);
  return (
    <div className="relative w-full" style={{ aspectRatio: `${W} / ${H}` }} data-testid="topology-diagram">
      <svg viewBox={`0 0 ${W} ${H}`} className="absolute inset-0 size-full" aria-hidden>
        <line x1={20} x2={W - 20} y1={ASYNC_Y} y2={ASYNC_Y} className="stroke-border" strokeDasharray="6 6" />
        <text x={24} y={ASYNC_Y - 8} className="fill-muted-foreground text-[13px]">
          synchronous request path
        </text>
        <text x={24} y={ASYNC_Y + 20} className="fill-muted-foreground text-[13px]">
          after COMMIT · asynchronous event path
        </text>
        {edges.map((e) => {
          const a = positions.get(e.from)!;
          const b = positions.get(e.to)!;
          if (e.curve) {
            const [x1, y1] = [a[0] - NODE_W / 2, a[1]];
            const [x2, y2] = [b[0] - NODE_W / 2, b[1] + 10];
            return (
              <path
                key={`${e.from}-${e.to}`}
                d={`M ${x1} ${y1} C ${x1 - 60} ${y1}, ${x2 - 60} ${y2}, ${x2} ${y2}`}
                fill="none"
                className="stroke-muted-foreground/40"
                strokeWidth={1.5}
                strokeDasharray="5 4"
              />
            );
          }
          const [x1, y1] = anchor(a, b);
          const [x2, y2] = anchor(b, a);
          return (
            <line
              key={`${e.from}-${e.to}`}
              x1={x1}
              y1={y1}
              x2={x2}
              y2={y2}
              className="stroke-muted-foreground/40"
              strokeWidth={1.5}
              strokeDasharray={e.async ? "5 4" : undefined}
            />
          );
        })}
      </svg>
      {components.map((c) => {
        const p = positions.get(c.id);
        if (!p) return null;
        return (
          <TopologyNode
            key={c.id}
            component={c}
            selected={c.id === selectedId}
            onSelect={() => onSelect(c.id)}
            className="absolute"
            style={{
              left: `${((p[0] - NODE_W / 2) / W) * 100}%`,
              top: `${((p[1] - NODE_H / 2) / H) * 100}%`,
              width: `${(NODE_W / W) * 100}%`,
              height: `${(NODE_H / H) * 100}%`,
            }}
          />
        );
      })}
    </div>
  );
}

/** Narrow screens: the same nodes as grouped lists (no graph to pan or squint at). */
export function TopologyList({
  components,
  selectedId,
  onSelect,
}: {
  components: ComponentHealth[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const groups: { title: string; match: (c: ComponentHealth) => boolean }[] = [
    { title: "Request path (synchronous)", match: (c) => ["gateway", "api", "cache", "database"].includes(c.kind) },
    { title: "Event path (after COMMIT)", match: (c) => ["worker", "broker"].includes(c.kind) },
    { title: "Telemetry", match: (c) => c.kind === "telemetry" },
  ];
  return (
    <div className="space-y-4">
      {groups.map((g) => {
        const items = components.filter(g.match);
        if (!items.length) return null;
        return (
          <section key={g.title} aria-label={g.title}>
            <h3 className="mb-2 text-xs font-medium text-muted-foreground">{g.title}</h3>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              {items.map((c) => (
                <TopologyNode key={c.id} component={c} selected={c.id === selectedId} onSelect={() => onSelect(c.id)} className="w-full" />
              ))}
            </div>
          </section>
        );
      })}
    </div>
  );
}
