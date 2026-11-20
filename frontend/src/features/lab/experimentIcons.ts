import { Gauge, KeyRound, Lock, Split, type LucideIcon } from "lucide-react";
import type { ExperimentId } from "@/types/api";

export const EXPERIMENT_ICON: Record<ExperimentId, LucideIcon> = {
  concurrency: Lock,
  idempotency: KeyRound,
  rate_limiting: Gauge,
  load_balancing: Split,
};

/** Short names and one-line purposes for selection cards and history rows. */
export const EXPERIMENT_COPY: Record<ExperimentId, { name: string; tagline: string; question: string }> = {
  concurrency: {
    name: "Concurrency",
    tagline: "Test simultaneous withdrawals",
    question: "Can many simultaneous withdrawals ever overspend one balance?",
  },
  idempotency: {
    name: "Idempotency",
    tagline: "Prevent duplicate transfers",
    question: "Does retrying the same request ever move money twice?",
  },
  rate_limiting: {
    name: "Rate Limiting",
    tagline: "Control requests across replicas",
    question: "Is one quota enforced across every replica?",
  },
  load_balancing: {
    name: "Load Balancing",
    tagline: "Distribute traffic across servers",
    question: "How does NGINX spread requests over the replicas?",
  },
};
