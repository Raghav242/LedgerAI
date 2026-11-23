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

export interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  /** Planned items are visibly marked and lead to a roadmap page, never a fake feature. */
  status: "live" | "planned";
  phase?: string;
  description: string;
}

export const NAV_ITEMS: NavItem[] = [
  { to: "/", label: "Overview", icon: LayoutDashboard, status: "live", description: "System snapshot from live backend data" },
  { to: "/demo", label: "Guided Demo", icon: Compass, status: "live", description: "Seven scenarios to walk through, step by step" },
  {
    to: "/playground",
    label: "Transaction Playground",
    icon: ArrowLeftRight,
    status: "live",
    description: "Create accounts, add simulated funds and run idempotent transfers",
  },
  { to: "/history", label: "Transaction History", icon: History, status: "live", description: "Every committed money movement" },
  {
    to: "/ledger",
    label: "Ledger & Reconciliation",
    icon: BookOpenCheck,
    status: "live",
    description: "Double-entry postings and invariant checks",
  },
  {
    to: "/experiments",
    label: "Experiment Lab",
    icon: FlaskConical,
    status: "live",
    description: "Run bounded concurrency, idempotency, rate-limit and load-balancing experiments",
  },
  { to: "/topology", label: "System Topology", icon: Network, status: "live", description: "Live component health, with the evidence behind each status" },
  { to: "/events", label: "Event Pipeline", icon: Workflow, status: "live", description: "Outbox, publisher, RabbitMQ and consumers, event by event" },
  { to: "/anomalies", label: "AI Anomaly Lab", icon: BrainCircuit, status: "live", description: "Run a scenario and see the anomaly model's asynchronous verdict" },
  { to: "/performance", label: "Performance Lab", icon: Gauge, status: "live", description: "Recorded throughput and latency across 1, 2 and 4 replicas" },
];
