# ADR 0004: Stateless replicas behind NGINX

Status: accepted · Phase: Phase 3

## Context

One process cannot use more than one machine's worth of CPU, and a crash takes the API down.

## Decision

API replicas hold no state (PostgreSQL and Redis hold it). NGINX round-robins, retries only
CONNECT failures (`proxy_next_upstream error timeout`), and never re-sends a POST that was
transmitted. Locally, containers get fixed IPs so a stopped replica fails fast instead of
costing a 7 s Docker DNS lookup.

## Consequences

+ Replicas are interchangeable; a killed replica loses no data (stack-tested).
+ Ambiguous mid-request failures surface to the client, which retries with the same key.
- Fixed IPs are a local workaround; production uses service discovery.
- Scaling is bounded by the shared PostgreSQL primary (see benchmarks).

## Alternatives considered

Least-connections (similar results for uniform requests); sticky sessions (unnecessary,
hide failures).
