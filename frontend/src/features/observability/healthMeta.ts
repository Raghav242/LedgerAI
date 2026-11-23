import { CircleHelp, CircleSlash, Clock, OctagonX, TriangleAlert, CheckCircle2, type LucideIcon } from "lucide-react";
import type { ComponentHealth, ComponentStatus } from "@/types/api";

/** How the UI shows a component status. "stale" is also used client-side when the snapshot itself is old. */
export const STATUS_META: Record<ComponentStatus, { label: string; icon: LucideIcon; text: string; dot: string; card: string; hint: string }> = {
  healthy: {
    label: "Healthy",
    icon: CheckCircle2,
    text: "text-success",
    dot: "bg-success",
    card: "border-success/40",
    hint: "Observed working just now.",
  },
  degraded: {
    label: "Degraded",
    icon: TriangleAlert,
    text: "text-warning",
    dot: "bg-warning",
    card: "border-warning/60 bg-warning/5",
    hint: "Running, but cannot do all of its work.",
  },
  unhealthy: {
    label: "Unhealthy",
    icon: OctagonX,
    text: "text-destructive",
    dot: "bg-destructive",
    card: "border-destructive/60 bg-destructive/5",
    hint: "Observed failing or unreachable.",
  },
  stale: {
    label: "Stale",
    icon: Clock,
    text: "text-muted-foreground",
    dot: "bg-muted-foreground/60",
    card: "border-dashed",
    hint: "The last report is too old to be trusted as current.",
  },
  unknown: {
    label: "Unknown",
    icon: CircleHelp,
    text: "text-muted-foreground",
    dot: "bg-muted-foreground/40",
    card: "border-dashed",
    hint: "No evidence either way.",
  },
  not_configured: {
    label: "Not configured",
    icon: CircleSlash,
    text: "text-muted-foreground",
    dot: "bg-transparent border border-muted-foreground/50",
    card: "border-dashed opacity-70",
    hint: "Not part of this deployment.",
  },
};

export const EVIDENCE_LABEL: Record<ComponentHealth["evidence"], string> = {
  probe: "Probed",
  heartbeat: "Self-reported",
  derived: "Derived",
  request: "Observed on this request",
  none: "No evidence",
};

/** "3 s ago", "2 min ago". */
export function ago(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "never";
  if (seconds < 1) return "just now";
  if (seconds < 90) return `${Math.round(seconds)} s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  return `${Math.round(seconds / 3600)} h ago`;
}

export function secondsSince(iso: string | null | undefined, now = Date.now()): number | null {
  if (!iso) return null;
  return Math.max(0, (now - new Date(iso).getTime()) / 1000);
}
