# ADR 0012: No Kafka, no Kubernetes

Status: accepted · Phase: Phase 6

## Context

The project must run on one laptop and be explainable end to end in an interview.

## Decision

RabbitMQ (single node, quorum queues) and Docker Compose. The designs name what production
would change: service discovery instead of fixed IPs, a broker cluster, managed PostgreSQL
with replicas, secrets from a vault.

## Consequences

+ Every mechanism is visible and testable locally.
- No partitioned log replay, no automatic rescheduling of failed containers across hosts.

## Alternatives considered

Kafka (ordering per partition and replay; heavier to run and to explain); Kubernetes
(real service discovery; far more setup than the demonstration needs).
