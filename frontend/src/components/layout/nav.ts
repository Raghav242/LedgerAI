import {
  ArrowLeftRight,
  BookOpenCheck,
  BrainCircuit,
  Compass,
  FlaskConical,
  Gauge,
  History,
  LayoutDashboard,
  Network,
  Workflow,
  type LucideIcon,
} from "lucide-react";

/** Sidebar sections, in display order: orient, move money, run experiments, observe. */
export const NAV_GROUPS = ["Start", "Money", "Labs", "Observe"] as const;
export type NavGroup = (typeof NAV_GROUPS)[number];

export interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  group: NavGroup;
  /** Planned items are visibly marked and lead to a roadmap page, never a fake feature. */
  status: "live" | "planned";
  phase?: string;
  description: string;
}

export const NAV_ITEMS: NavItem[] = [
  { to: "/", label: "Overview", icon: LayoutDashboard, group: "Start", status: "live", description: "System snapshot from live backend data" },
  { to: "/demo", label: "Guided Demo", icon: Compass, group: "Start", status: "live", description: "Seven scenarios to walk through, step by step" },
  {
    to: "/playground",
    label: "Transaction Playground",
    icon: ArrowLeftRight,
    group: "Money",
    status: "live",
    description: "Create accounts, add simulated funds and run idempotent transfers",
  },
  { to: "/history", label: "Transaction History", icon: History, group: "Money", status: "live", description: "Every committed money movement" },
  {
    to: "/ledger",
    label: "Ledger & Reconciliation",
    icon: BookOpenCheck,
    group: "Money",
    status: "live",
    description: "Double-entry postings and invariant checks",
  },
  {
    to: "/experiments",
    label: "Experiment Lab",
    icon: FlaskConical,
    group: "Labs",
    status: "live",
    description: "Run bounded concurrency, idempotency, rate-limit and load-balancing experiments",
  },
  { to: "/anomalies", label: "AI Anomaly Lab", icon: BrainCircuit, group: "Labs", status: "live", description: "Run a scenario and compare the model, the rules and their evaluation" },
  { to: "/performance", label: "Performance Lab", icon: Gauge, group: "Labs", status: "live", description: "Measured throughput, latency and contention across 1, 2 and 4 replicas" },
  { to: "/topology", label: "System Topology", icon: Network, group: "Observe", status: "live", description: "Live component health, with the evidence behind each status" },
  { to: "/events", label: "Event Pipeline", icon: Workflow, group: "Observe", status: "live", description: "Outbox, publisher, RabbitMQ and consumers, event by event" },
];
