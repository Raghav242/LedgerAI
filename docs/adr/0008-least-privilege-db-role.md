# ADR 0008: Separate migration and runtime database roles

Status: accepted · Phase: Phase 6B

## Context

The application connected as the schema owner: any bug or injection could alter the
schema, truncate the ledger, or delete audit rows.

## Decision

The `migrate` job (owner) runs Alembic and then `app.core.db_roles`, which creates the
runtime role, grants exactly the privileges each table needs, verifies them, and fails on
any table without a policy. Replicas and workers connect as the runtime role.

## Consequences

+ Ledger and audit tables are append-only at the privilege level.
+ Retention can only delete lab and telemetry rows.
+ The integration suite runs as the runtime role, so missing grants fail tests.
- Grants belong to tables: a schema rebuild needs the role setup re-run (the migrate job does).

## Alternatives considered

Row-level security (unnecessary for a single tenant); keeping one role (status quo).
