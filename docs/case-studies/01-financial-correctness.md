# Case Study 1: Financial Correctness Under Concurrency

## Problem

A transfer is "read balance, check, write balance". With two replicas and many clients, two
transfers from the same account can both read $100, both pass a $60 check, and both commit:
$120 leaves a $100 account. Retries make it worse. A client that times out cannot tell
whether its transfer happened, and retrying blindly can execute it twice.

## Constraints

- Several stateless API replicas behind a load balancer: no in-process locks.
- Clients retry, and responses can be lost after COMMIT.
- Every cent must be explainable afterwards (auditable).

## Design

1. **Lock, then decide** ([ADR 0001](../adr/0001-pessimistic-row-locks.md)).
   - One transaction per transfer.
   - Both account rows are locked with `SELECT … FOR UPDATE` in ascending id order.
   - The funds check runs under the lock that protects the write.
   - `lock_timeout = 3 s` turns a long wait into a retryable 503, so nothing hangs and
     nothing half-commits.
2. **One outcome per Idempotency-Key** ([ADR 0002](../adr/0002-durable-idempotency.md)).
   - The key is claimed in the same transaction, and the exact response is stored before
     COMMIT.
   - A retry replays the stored response. A different body under the same key is 422. A
     concurrent twin waits for the first request and then replays it.
   - When the connection is lost during COMMIT, the answer is `OUTCOME_UNKNOWN`: retrying
     the same key reveals the truth.
3. **Double-entry ledger with three enforcement layers**
   ([ADR 0003](../adr/0003-double-entry-ledger.md), [ADR 0008](../adr/0008-least-privilege-db-role.md)).
   - Application: postings are written with the balances.
   - Database triggers: postings must balance at COMMIT and are immutable.
   - Privileges (Phase 6B): the runtime role cannot UPDATE or DELETE ledger rows at all.

## Evidence (all automated, all reproducible)

| Scenario | Expected | Where |
|---|---|---|
| 100 concurrent $10 transfers from a $100 account | exactly 10 succeed, balance 0, ledger reconciles | `scripts/demo_concurrency.py`, `tests/integration/test_concurrency.py`, Experiment Lab "Concurrency" |
| 20 concurrent copies of one request (same key) | one transfer, 20 identical responses | `scripts/demo_idempotency.py`, `tests/integration/test_idempotency.py` |
| Retries of one key land on different replicas | one transfer | `tests/stack/test_multi_replica.py` |
| A replica is `docker kill`ed while 40 keyed transfers are in flight | all 40 eventually 201, exactly 40 transfers, balance and reconciliation exact | `test_killing_a_replica_mid_traffic_does_not_corrupt_state` |
| PostgreSQL, Redis and a replica restart | committed money and idempotency records survive; the retried key replays | `test_restarts_preserve_committed_money_and_idempotency` (6B) |
| Runtime role tries `UPDATE ledger_entries`, `DELETE FROM transfers`, `TRUNCATE` | permission denied | `tests/integration/test_db_roles.py` (6B) |
| Backup restored into a throwaway server | row counts equal, postings sum to 0, every balance = its postings | `scripts/db_backup.py restore-drill`, stack test (6B) |

## Trade-offs

- **A hot account serializes.**
  - Every transfer from one account waits for the previous one's lock, so throughput from
    that account is one transaction at a time, regardless of replica count.
  - The hot-account benchmark measures it (case study 3).
  - Alternatives with their own costs: sharding balances (sub-accounts), or batching
    debits.
- **Idempotency records accumulate.** There is no expiry yet. Expiring them is safe only
  after the client-side retry window, which is a product decision.
- **Pessimistic over optimistic.**
  - Under contention, optimistic version checks turn into retry storms.
  - Locks turn contention into short, bounded waits with a clear 503 when the bound is
    hit.

## What I would change at scale

Per-account sharding of hot balances, a read replica for history and reconciliation,
idempotency-record expiry with a documented retry window, and serializable isolation for
any future multi-row invariants that row locks cannot express.
