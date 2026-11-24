# Interview Guide

Everything below is grounded in this repository: every number comes from a test, a demo
or a recorded benchmark, and every claim names where to see it.

## 1. The 60-second introduction

> LedgerFlow AI is a virtual-money payments backend I built to practise the hard parts of
> distributed systems, and to prove them with tests rather than claim them.
>
> Transfers are double-entry and concurrency-safe: both accounts are locked in id order in
> one PostgreSQL transaction. Durable idempotency keys make retries safe across replicas.
> The ledger is append-only at three levels: code, triggers and database privileges.
>
> It scales horizontally: stateless FastAPI replicas behind NGINX, with a global rate limit
> in Redis that fails closed for money. Events leave through a transactional outbox to
> RabbitMQ. Consumers are idempotent, with bounded retries and per-consumer dead-letter
> queues.
>
> An Isolation Forest worker scores transfers asynchronously. I found it was blind to
> rapid bursts, fixed it with per-account velocity features and per-family forests, and
> evaluated it honestly against a rule baseline, including on shifted data.
>
> There is a React lab that runs real experiments against the stack: concurrency,
> idempotency, outages, benchmarks. CI covers tests, type drift, dependency audits, secret
> and container scanning.

## 2. The 5-minute demo

Setup: `docker compose up -d --build --wait`, `cd frontend; npm run dev`, and optionally
`python -m labctl --enable-failures`. Open http://127.0.0.1:5173.

| Minute | Show | Say |
|---|---|---|
| 0:00 | **Overview** → System Topology | "Every status has evidence: probed, heartbeat, or derived. The broker is probed by a monitoring-only user that cannot touch messages." |
| 0:45 | **Experiment Lab → Concurrency** (100 concurrent $10 from $100) | "Exactly 10 succeed, never 11. Row locks in id order, funds check under the lock, ledger reconciles to zero." |
| 1:45 | **Experiment Lab → Idempotency** | "Twenty copies of one request, one transfer. The key is claimed in the same transaction as the money movement, so it survives crashes and replica hops." |
| 2:30 | **Failure Lab → RabbitMQ outage** (with the controller running) | "Transfers keep committing while the broker is down. Events wait in the outbox and arrive exactly once afterwards. The controller is local-only and allow-listed: no API can run Docker." |
| 3:30 | **AI Lab → Rapid burst** | "The v1 model missed every burst. v2 flags it, and you can see the model's and the rules' verdicts separately, plus how both behave on data they never saw." |
| 4:30 | **Performance Lab** | "Measured, repeated runs: read throughput scales with replicas until CPU is the limit; a hot account plateaus at about 100 req/s, because one row lock serializes it." |

## 3. The 15-minute deep dive (pick by interviewer interest)

1. **Correctness core (4 min)**: [case study 1](case-studies/01-financial-correctness.md).
   Walk the transfer sequence in [lld.md](architecture/lld.md#2-transfer-the-critical-path),
   then the idempotency outcome table, `OUTCOME_UNKNOWN`, and the three-layer ledger
   enforcement.
2. **Reliability (4 min)**: [case study 2](case-studies/02-event-reliability.md). Outbox
   leases and fencing tokens; receipts with ack after COMMIT. Then the DLQ bug and its
   policy-based fix, which had to work around immutable queue arguments.
3. **Scaling (4 min)**: [case study 3](case-studies/03-scaling-performance.md). Why the
   quota is global, why writes fail closed, how the benchmark is isolated and repeated,
   and what the hot-account numbers mean.
4. **ML judgement (3 min)**: [ADR 0010](adr/0010-anomaly-model.md) and the AI Lab model
   card. The root cause of zero burst recall, the dilution in a single forest, choosing
   thresholds on validation only, and why the rule baseline "wins" in-distribution.

## 4. Questions and answers

**1. How do you prevent double spending?**
Each transfer runs in one PostgreSQL transaction that locks both account rows with
`SELECT … FOR UPDATE` before checking the balance. Two concurrent transfers from one
account serialize on that row, so the second sees the first's debit. Tested with 100
threads: exactly 10 of 10 affordable transfers succeed.

**2. Why lock in id order?**
Transfers A→B and B→A taken in request order can deadlock. Locking the lower id first makes
both wait on the same row, so it's a short queue instead of a deadlock.

**3. Optimistic vs pessimistic locking?**
Under contention, optimistic version checks fail and retry repeatedly, which turns into a
retry storm. Locks queue, bounded by `lock_timeout = 3 s`, and return a retryable 503
instead of hanging. For a hot account, both approaches serialize; locks just do it fairly.

**4. What makes your idempotency "durable"?**
The key is a unique row claimed in the same transaction as the transfer, holding the exact
response. Crashes, restarts and replica switches can't produce a second effect. A
Redis-based cache can't give that guarantee, because it isn't atomic with the commit.

**5. What if the connection drops during COMMIT?**
The server may or may not have committed. `atomic()` detects it and returns 500
`OUTCOME_UNKNOWN`. Retrying the same key reveals the truth: a replay if it committed, one
execution if not. Never retry with a new key.

**6. Why store business rejections under the key?**
A key names one attempt, and that attempt's outcome is final. Otherwise "insufficient
funds" followed by a top-up would silently turn a retry into a different transfer.

**7. How do you know the ledger is right?**
- Every transfer writes a debit and a credit that sum to zero.
- Deferred triggers verify this at COMMIT and reject edits.
- The runtime role has no UPDATE/DELETE on ledger tables.
- `/reconciliation` recomputes every balance from postings, after every suite and demo, and
  in the restore drill.

**8. Why NGINX retries only connect errors?**
A failed connect means the request never ran, so retrying elsewhere is safe. A POST that was
transmitted may have executed. Re-sending it could duplicate it, so NGINX surfaces the
error and the client retries with its Idempotency-Key.

**9. Why is the rate limit in Redis, and why fail closed?**
Per-replica limits multiply with the replica count. A Lua token bucket in Redis is global
and atomic: the stack test admits exactly one bucket's worth across two replicas. If Redis
is down, money movement stops (503) rather than becoming unlimited. Reads fail open.

**10. What happens when Redis restarts?**
Buckets refill, giving a bounded over-admission of one burst. No financial data lives in
Redis.

**11. What is the dual-write problem and how does the outbox solve it?**
Writing to the database and the broker separately can lose an event, or emit one for a
rolled-back transfer. The event row commits with the transfer, and a publisher ships it
later. A broker outage only delays events.

**12. Can the publisher send an event twice?**
Yes, if it crashes after the broker's confirm but before marking the row. The lease expires
and another publisher resends. Delivery is at-least-once, and consumers deduplicate.

**13. How do consumers get exactly-once effects?**
They insert a receipt `(event_id, consumer)` in the same transaction as the effect, and ack
only after COMMIT. A redelivery hits the receipt's primary key and is acknowledged with no
second effect.

**14. How do you handle poison messages?**
- Transient errors go through TTL retry tiers (1 s, 5 s, 25 s).
- Permanent errors go straight to the consumer's DLQ.
- A message that crashes the process on every delivery is dead-lettered by the quorum
  queue's delivery limit.

**15. Tell me about a bug you found late.**
Both consumers' DLQs were bound to one fanout exchange, so each failure appeared in every
DLQ. Queue arguments are immutable, so I couldn't just change the dead-letter exchange
without recreating queues that held messages.

I kept the fanout and routed it into a direct exchange. Each consumer sets the dead-letter
routing key with a broker policy, and a headers exchange was ruled out because it ignores
`x-` headers. I migrated the live broker without losing the existing dead letter, and added
real-broker tests.

**16. How would you scale this to 100× traffic?**
- First, measure: the benchmarks show the replicas' CPU is the limit on one machine, and
  the database is next.
- Then: more API hosts, PostgreSQL connection pooling (PgBouncer), read replicas for history
  and reconciliation, partitioning or archiving old transfers.
- Hot accounts need a different model (sub-account sharding or batching), because a row lock
  doesn't scale.

**17. Why not Kafka?**
Kafka would buy partitioned ordering and replay at a big operational cost. Events here need
neither strict order nor replay; RabbitMQ quorum queues give durable at-least-once delivery
and retries cheaply. With Kafka I'd still need the outbox and idempotent consumers.

**18. How do you keep benchmarks honest?**
- Separate Compose project, verified not to touch dev data.
- Warmup, then 3 repeats per configuration, reporting mean ± standard deviation.
- The load generator runs in a container on the stack network.
- Resource sampling, nothing extrapolated.
- The page states the machine and that everything shares one laptop.

**19. Your anomaly model had zero burst recall. Why, and what did you do?**
Burst detection relied on one extreme feature, the gap to the previous transfer. An
Isolation Forest picks a random feature per split, so one extreme feature out of six barely
shortens the isolation path.

I added cadence-relative features and gave each feature family its own forest. Burst recall
went from 0.00 to 1.00 on unseen accounts, and 0.88 when bursts were slowed, where the fixed
rule caught 0.30.

**20. Then why does the rule baseline have higher F1?**
The rules restate how the synthetic generator creates anomalies, so in-distribution they are
near an upper bound. The unsupervised model pays about 1% false positives. Its value shows
under shift. I deployed model-OR-rules because it won on validation data, and I never tuned
on the test worlds.

**21. How do you avoid training/serving skew?**
- One `compute_features` function builds the training data and the live features.
- An integration test asserts the worker's SQL statistics equal the training replay's.
- Features use only transfers strictly before the scored one (a leakage test appends future
  data and checks nothing changes).
- Inputs are schema-validated, and invalid ones are dead-lettered, never scored.

**22. How would you detect model decay?**
`/ai/drift` computes the Population Stability Index of recent live features against the
training deciles, and the live flag rate against the validation rate. Lab traffic is
deliberately unusual, so drift there is expected. In production this would alert, and
labelled outcomes would measure decay directly.

**23. What security decisions matter most?**
- No API can touch Docker; the infrastructure controller is local, tokenized and
  allow-listed.
- The development surface (minting money) refuses to start in production or on a public
  bind.
- A least-privilege DB role makes the ledger append-only.
- The lab's HMAC key is independent of other credentials and isn't treated as user
  authentication.
- Secrets never enter images.
- CI runs pip-audit, npm audit, gitleaks and Trivy.

**24. What would you need before a public launch?**
Authentication and per-principal quotas (the principal seam exists), TLS, a secret store,
backup schedules, and alerting. The [public-demo design](public-demo.md) lists it and
provides a production-mode Compose profile that already refuses the dev surface.

**25. What are the known limitations?**
- Single-node PostgreSQL, Redis and RabbitMQ.
- Fixed container IPs as a local workaround.
- No authentication.
- Idempotency records never expire.
- The AI uses synthetic data only.
- Hot accounts serialize.

See [phase-6b-6d.md](phase-6b-6d.md#remaining-risks).
