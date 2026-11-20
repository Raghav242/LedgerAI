import { CheckCircle2, CircleDashed, Loader2, OctagonAlert, XCircle } from "lucide-react";
import type { ReactNode } from "react";
import type { LabRunStatus } from "@/types/api";

export const RUN_STATUS: Record<
  LabRunStatus,
  { label: string; variant: "success" | "destructive" | "warning" | "info" | "muted"; icon: ReactNode; description: string }
> = {
  queued: { label: "Queued", variant: "muted", icon: <CircleDashed />, description: "Waiting for the runner to start." },
  running: { label: "Running", variant: "info", icon: <Loader2 className="motion-safe:animate-spin" />, description: "Sending traffic and recording observations." },
  passed: { label: "Passed", variant: "success", icon: <CheckCircle2 />, description: "Completed, and every applicable invariant held." },
  failed: { label: "Invariant violated", variant: "destructive", icon: <XCircle />, description: "Completed, but at least one invariant did not hold." },
  error: { label: "Did not complete", variant: "warning", icon: <OctagonAlert />, description: "The run could not finish (timeout, unreachable target or runner stopped)." },
};
