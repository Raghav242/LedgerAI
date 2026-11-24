# ADR 0001: Pessimistic row locks for transfers

Status: accepted · Phase: Phase 2

## Context

Concurrent transfers from one account raced: two requests read the same balance, both
passed the funds check, both debited (lost update / double spend).

## Decision

Each transfer runs in ONE PostgreSQL transaction at READ COMMITTED and locks both accounts
with `SELECT ... FOR UPDATE` in ascending id order before checking funds. `SET LOCAL
lock_timeout = 3 s` bounds waiting; a timeout becomes 503 `CONCURRENCY_CONFLICT` (nothing
committed, retry with the same key).

## Consequences

+ Correct by construction; the check happens under the lock that protects the write.
+ Ordered locking turns would-be deadlocks into short waits.
- A hot account serializes its transfers (measured in the hot-account benchmark).
- Throughput per account is bounded by one transaction at a time.

## Alternatives considered

Optimistic version columns (retry storms under contention); SERIALIZABLE (aborts instead
of waits, more client retries); application-level mutex (not distributed, breaks with
replicas).
