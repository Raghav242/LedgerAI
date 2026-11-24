# ADR 0009: Isolated Compose projects for benchmarks and stack tests

Status: accepted · Phase: Phase 6A/6B

## Context

Benchmarks and stack tests used to redeploy the development stack against other databases,
sharing its broker queues and Redis, and could leave it misconfigured on failure.

## Decision

Run them as separate Compose projects (`ledgerflow-bench`, `ledgerflow-stacktest`) from the
same compose file with their own container prefix, network, volumes, ports and database.
`assert_isolated()` refuses any overlap before a Docker call; projects are removed with
their volumes afterwards.

## Consequences

+ Verified: a benchmark run left the development database and containers byte-identical.
+ Stack tests run while the development stack keeps running.
- Two stacks on one laptop compete for CPU and memory (affects measurements, not correctness).

## Alternatives considered

A second compose file (duplication); Testcontainers (re-implements the topology).
