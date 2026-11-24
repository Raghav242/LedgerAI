# Architecture Decision Records

Short records of decisions that shaped the system: context, decision, consequences and the
alternatives considered. Status: all accepted.

| ADR | Decision | Phase |
|---|---|---|
| [0001](0001-pessimistic-row-locks.md) | Pessimistic row locks for transfers | Phase 2 |
| [0002](0002-durable-idempotency.md) | Durable idempotency in the transfer's transaction | Phase 2 |
| [0003](0003-double-entry-ledger.md) | Double-entry ledger with database-enforced invariants | Phase 2 |
| [0004](0004-stateless-replicas-nginx.md) | Stateless replicas behind NGINX | Phase 3 |
| [0005](0005-redis-token-bucket.md) | Distributed token bucket in Redis; writes fail closed | Phase 3 |
| [0006](0006-transactional-outbox.md) | Transactional outbox with a polling publisher | Phase 4A |
| [0007](0007-per-consumer-dead-letters.md) | Per-consumer dead-letter routing via a broker policy | Phase 6B |
| [0008](0008-least-privilege-db-role.md) | Separate migration and runtime database roles | Phase 6B |
| [0009](0009-isolated-compose-projects.md) | Isolated Compose projects for benchmarks and stack tests | Phase 6A/6B |
| [0010](0010-anomaly-model.md) | Unsupervised per-family Isolation Forests plus rules (hybrid) | Phase 5E/6B |
| [0011](0011-local-only-controller.md) | Infrastructure control only in a local, allow-listed controller | Phase 5F |
| [0012](0012-scope-no-kafka-no-k8s.md) | No Kafka, no Kubernetes | Phase 6 |
