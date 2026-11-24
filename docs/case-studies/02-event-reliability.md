# Case Study 2: Event-Driven Reliability and Failure Recovery

## Problem

A committed transfer must reliably produce an event for downstream consumers: an audit log
and an anomaly detector. Two naive options both fail:
- **Publish after COMMIT:** a crash in between loses the event.
- **Publish inside the transaction:** a rollback leaves a phantom event.

On top of that, brokers redeliver, consumers crash halfway, and one bad message can crash
a consumer forever.

## Design

1. **Transactional outbox** ([ADR 0006](../adr/0006-transactional-outbox.md)).
   - The event row commits with the transfer.
   - A publisher claims rows with `FOR UPDATE SKIP LOCKED` and a 30 s lease, publishes
     with confirms and `mandatory`, and marks rows published only under its own claim
     token (fencing against a stalled publisher).
2. **Idempotent consumers.**
   - Each consumer writes a receipt `(event_id, consumer)` in the same transaction as its
     effect, and acknowledges only after COMMIT.
   - A redelivery after a crash finds the receipt and does nothing.
3. **Bounded retries, no hot loop.**
   - Transient failures go to TTL retry queues (1 s, 5 s, 25 s) that dead-letter back into
     the main queue.
   - Permanent failures, invalid events and exhausted retries go to the consumer's DLQ.
   - A message that crashes the consumer on every delivery is dead-lettered by the broker
     (quorum queue `x-delivery-limit`).
4. **Per-consumer DLQs** (Phase 6B, [ADR 0007](../adr/0007-per-consumer-dead-letters.md)).
   - **The bug:** both consumers' DLQs were bound to one fanout DLX, so one consumer's
     failure appeared in every DLQ.
   - **The constraint:** queue arguments are immutable, so the fix keeps every queue and
     changes only routing:
     - a direct router behind the fanout;
     - a per-queue policy that sets the dead-letter routing key;
     - consumer dead letters published straight to the consumer's own DLQ;
     - an alternate exchange, so nothing unattributable is ever dropped.
5. **Independent observability** (6A).
   - A management-API probe with a monitoring-only user, whose permissions match nothing,
     reports broker health and every DLQ depth.
   - It keeps working even when all workers are down.

## Evidence

| Failure | Behaviour | Where |
|---|---|---|
| RabbitMQ stopped for minutes | transfers keep committing; the outbox drains after restart; each event applied once | `scripts/demo_outbox_recovery.py`, failure lab "RabbitMQ outage", `test_broker_outage.py` |
| Publisher crashes after the broker acked, before marking published | lease expires, event republished, consumer deduplicates | `test_crash_after_broker_confirm_before_outbox_update_republishes_safely` |
| Consumer process dies before / after its commit | redelivered, effect exactly once | `scripts/demo_worker_crash.py`, failure lab "Worker crash", `test_crash_recovery.py` |
| One event delivered 7 times | applied once | `scripts/demo_event_deduplication.py` |
| Poison message crashes the consumer on every delivery | broker dead-letters it after the delivery limit, into **that consumer's** DLQ only | `test_dead_letter_routing.py` (6B, real broker) |
| Two consumers each reject the same malformed event | one copy in each DLQ, never two | `test_consumer_dead_letters_reach_only_its_own_dlq` |
| Migration on a broker with the old fanout and messages already dead-lettered | existing DLQ messages preserved; new failures routed correctly | `test_migration_from_the_shared_fanout_keeps_existing_dead_letters`, plus the live development broker (1 pre-existing dead letter kept) |
| Consumer starts without the management API | broker dead letters kept in `ledgerflow.events.unrouted.dlq`, never dropped | `test_without_the_policy_broker_dead_letters_are_kept_unrouted` |

## Trade-offs

- **At-least-once, not exactly-once delivery.**
  - Exactly-once effect comes from deduplication, which every new consumer must implement.
  - The shared `EventConsumer` does it for them.
- **No ordering guarantee across events.** Consumers must not depend on order (the audit
  log and the AI worker don't).
- **Polling publisher.** Simple and observable, with latency of about one poll interval.
  CDC would remove polling, at the cost of another service.
- **Single-node broker.** It survives restarts (durable quorum queues, persistent messages),
  but not the loss of its disk. Unpublished events are always safe in PostgreSQL.

## What I would change at scale

A three-node RabbitMQ cluster (quorum queues already replicate), CDC from the outbox, DLQ
alerting with automated replay tooling, and per-event-type consumer queues for independent
scaling.
