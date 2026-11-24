# ADR 0005: Distributed token bucket in Redis; writes fail closed

Status: accepted · Phase: Phase 3

## Context

Per-replica limits multiply with the replica count; a global quota needs shared state.

## Decision

One Lua script refills and takes tokens atomically per (principal, policy) key. If Redis is
unreachable (250 ms timeouts), financial writes fail CLOSED with 503 and reads fail OPEN.
Rate-limited requests never reach the transaction service (their keys are never claimed).

## Consequences

+ The quota is global across replicas (stack-tested: exactly one bucket's worth admitted).
+ Redis outage cannot let unlimited money movement through.
- Redis restart refills buckets (bounded over-admission of one burst).

## Alternatives considered

Fixed window (bursts at window edges); PostgreSQL counters (load on the primary);
fail-open writes (unsafe).
