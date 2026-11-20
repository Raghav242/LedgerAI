import {
  ArrowLeftRight,
  BookOpenCheck,
  BrainCircuit,
  FlaskConical,
  Gauge,
  History,
  LayoutDashboard,
  Network,
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
  { to: "/topology", label: "System Topology", icon: Network, status: "live", description: "Architecture and request flows (static; live status in 5C)" },
  { to: "/anomalies", label: "AI Anomaly Lab", icon: BrainCircuit, status: "planned", phase: "4B", description: "Isolation Forest anomaly detection (backend not built yet)" },
  { to: "/performance", label: "Performance Lab", icon: Gauge, status: "planned", phase: "5D", description: "Benchmarks across replica counts and configurations" },
];
