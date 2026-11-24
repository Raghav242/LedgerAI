# ADR 0006: Transactional outbox with a polling publisher

Status: accepted · Phase: Phase 4A

## Context

Publishing to RabbitMQ after COMMIT can lose events (crash in between); publishing before
COMMIT can emit events for rolled-back transfers (the dual-write problem).

## Decision

The transfer's transaction inserts the event into `outbox_events`. A publisher claims due
rows with `FOR UPDATE SKIP LOCKED` and a lease, publishes with confirms and `mandatory`,
and marks rows published only if it still holds the claim token (fencing).

## Consequences

+ No lost events, no phantom events; the broker can be down for hours.
+ Several publishers run safely.
- At-least-once: consumers must deduplicate (they do, by event id).
- Polling adds latency (≈ poll interval) and load proportional to the backlog.

## Alternatives considered

CDC/Debezium (an extra service); two-phase commit (not supported by RabbitMQ, fragile).
