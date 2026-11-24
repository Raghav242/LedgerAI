# ADR 0007: Per-consumer dead-letter routing via a broker policy

Status: accepted · Phase: Phase 6B

## Context

Every main queue named one shared FANOUT dead-letter exchange, so a message that failed in
one consumer was copied into every consumer's DLQ. Queue arguments (the DLX) are immutable,
so changing them would mean deleting queues that may hold messages.

## Decision

Keep every queue and the fanout. Bind the fanout to a new DIRECT router whose bindings are
`<queue>.dlq` by name, with an alternate exchange into `ledgerflow.events.unrouted.dlq`.
Each consumer applies a policy setting `dead-letter-routing-key` for its own queue (policy
keys merge with queue arguments) and publishes its own dead letters straight to its DLQ.
Legacy fanout bindings are removed by the idempotent topology declaration.

## Consequences

+ Failures stay with the consumer that failed; existing DLQ messages were preserved in the
live migration (verified on the development broker).
+ Nothing is ever dropped: unattributable dead letters land in the unrouted DLQ.
- Correct broker routing needs the management API to apply the policy (the consumer logs a
warning and dead letters are kept in the unrouted DLQ until it is applied).

## Alternatives considered

Headers exchange on `x-last-death-queue` (headers exchanges ignore `x-` headers); new
queues with new arguments (requires draining and renaming live queues).
