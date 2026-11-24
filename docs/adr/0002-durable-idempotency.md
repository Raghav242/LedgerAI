# ADR 0002: Durable idempotency in the transfer's transaction

Status: accepted · Phase: Phase 2

## Context

Clients retry after timeouts. Without idempotency a retried transfer can execute twice; an
in-memory cache does not survive restarts or span replicas.

## Decision

`idempotency_records (scope, key)` is UNIQUE; the key is claimed with `INSERT ... ON
CONFLICT DO NOTHING` inside the same transaction as the transfer, and completed with the
exact response body before COMMIT. Same key + same body replays the stored response; same
key + different body is 422; an in-flight twin waits on the row lock (409 if too long).
The scope comes from the server-side principal, never from the client.

## Consequences

+ Exactly one effect per key across replicas, restarts and crashes.
+ Ambiguous commits (connection lost during COMMIT) are resolved by retrying the same key.
- One extra row and index lookup per write; records are kept indefinitely (no expiry yet).

## Alternatives considered

Redis SETNX cache (not atomic with the database commit); request hashing without keys
(cannot tell a retry from a deliberate repeat).
