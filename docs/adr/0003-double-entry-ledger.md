# ADR 0003: Double-entry ledger with database-enforced invariants

Status: accepted · Phase: Phase 2

## Context

A single `balance` column cannot explain how money moved or prove nothing was lost.

## Decision

Every transfer writes two immutable postings (debit, credit) that sum to zero, in the same
transaction as the balance updates. Deferred constraint triggers verify balance at COMMIT,
row triggers reject UPDATE/DELETE, and (Phase 6B) the runtime role has no UPDATE/DELETE
grant on the ledger. A reconciliation endpoint recomputes every balance from postings.

## Consequences

+ Auditable history; reconciliation after every test suite and demo.
+ Invariants hold even against buggy application code.
- More writes per transfer; schema changes need migrations that keep the ledger balanced.

## Alternatives considered

Balance-only accounts (no audit trail); event sourcing (more moving parts than needed).
