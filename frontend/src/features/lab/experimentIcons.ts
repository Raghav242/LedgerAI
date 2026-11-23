import { BrainCircuit, Cog, DatabaseZap, Gauge, KeyRound, ListX, Lock, ServerOff, Split, type LucideIcon } from "lucide-react";
import type { ExperimentId } from "@/types/api";

export const EXPERIMENT_ICON: Record<ExperimentId, LucideIcon> = {
  concurrency: Lock,
  idempotency: KeyRound,
  rate_limiting: Gauge,
  load_balancing: Split,
  redis_outage: DatabaseZap,
  rabbitmq_outage: ListX,
  worker_crash: Cog,
  replica_failure: ServerOff,
  anomaly_scenario: BrainCircuit,
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
  redis_outage: {
    name: "Redis Outage",
    tagline: "Writes fail closed, then recover",
    question: "What happens to money movements when the rate limiter's Redis disappears?",
  },
  rabbitmq_outage: {
    name: "RabbitMQ Outage",
    tagline: "Outbox keeps events safe",
    question: "Can transfers commit while the broker is down, without losing their events?",
  },
  worker_crash: {
    name: "Worker Crash",
    tagline: "Redelivery without double effects",
    question: "If the consumer dies mid-event, is the event lost or applied twice?",
  },
  replica_failure: {
    name: "Replica Failure",
    tagline: "Failover through NGINX",
    question: "What do clients see when one API replica stops, and how does it rejoin?",
  },
  anomaly_scenario: {
    name: "AI Scenario",
    tagline: "Model verdict on real activity",
    question: "What does the anomaly model say about this activity, and how did the event reach it?",
  },
};
