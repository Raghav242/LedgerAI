# Case Study 3: Horizontal Scaling and Performance

## Question

What does adding API replicas buy, where does it stop helping, and how do we know the
numbers are real?

## Method (so the numbers can be trusted)

- **Isolated target.** Compose project `ledgerflow-bench`: its own containers, network,
  volumes (its own PostgreSQL), Redis and ports. It is created fresh and removed afterwards.
  An earlier verification run left the development database and every dev container
  byte-identical ([ADR 0009](../adr/0009-isolated-compose-projects.md)).
- **Load generator.** Locust 2.47 in a container on the benchmark network, 4 worker
  processes, 32 closed-loop users (no think time), accounts pre-provisioned outside the
  measured window.
- **Repeatability.** For every (replicas, workload): a 10 s unmeasured warmup, then 3
  measured runs of 20 s, reported as mean ± standard deviation, with min–max kept.
  Resources are sampled mid-run with `docker stats`.
- **Workloads.**
  - *Read-heavy:* account, balance and list reads.
  - *Mixed transfers:* each user transfers between its own two accounts, plus balance
    reads.
  - *Hot account:* every user debits **the same** account.
- **Honesty rules.** Nothing extrapolated. A run disturbed by other work on the machine was
  discarded and rerun (the first full run overlapped a secret scan).
- **Machine.** One laptop: 12 CPUs, 7.6 GiB for the Docker VM, shared by the stack and the
  load generator. Absolute numbers are machine-specific; the shape is the finding.

Run: `python benchmarks/run_benchmarks.py --duration 20 --users 32 --replicas 1 2 4 --repeats 3 --warmup 10`
→ `benchmarks/results/20261123-182416.{json,md}`.

## Results

| Workload | Replicas | Req/s (mean ± std) | min–max | Scaling eff. | p50 | p95 (± std) | p99 | Errors | PG conns max | CPU % mid-run (API avg / PostgreSQL) |
|---|---|---|---|---|---|---|---|---|---|---|
| Read-heavy | 1 | 252 ± 5 | 247–256 | 100% | 130 ms | 213 ± 6 ms | 280 ms | 0.00% | 10 | 124 / 15 |
| Read-heavy | 2 | 361 ± 45 | 324–411 | 72% | 88 ms | 167 ± 38 ms | 223 ms | 0.00% | 20 | 144 / 54 |
| Read-heavy | 4 | 598 ± 26 | 580–628 | 59% | 43 ms | 133 ± 12 ms | 180 ms | 0.00% | 29 | 120 / 72 |
| Mixed transfers | 1 | 68 ± 7 | 63–76 | 100% | 447 ms | 740 ± 125 ms | 990 ms | 0.00% | 10 | 131 / 34 |
| Mixed transfers | 2 | 118 ± 6 | 113–124 | 86% | 243 ms | 563 ± 95 ms | 730 ms | 0.00% | 20 | 130 / 63 |
| Mixed transfers | 4 | 205 ± 5 | 200–211 | 75% | 105 ms | 443 ± 100 ms | 580 ms | 0.00% | 33 | 112 / 121 |
| Hot account | 1 | 54 ± 11 | 42–63 | 100% | 410 ms | 1333 ± 1013 ms | 4000 ms | 0.00% | 10 | 94 / 29 |
| Hot account | 2 | 102 ± 1 | 101–102 | 93% | 173 ms | 640 ± 26 ms | 3567 ms | 0.00% | 20 | 72 / 42 |
| Hot account | 4 | 105 ± 4 | 101–109 | 48% | 147 ms | 1067 ± 58 ms | 1667 ms | 0.00% | 40 | 37 / 61 |

Scaling efficiency = mean req/s with N replicas / (N × mean req/s with 1 replica). CPU:
100% = one core.

Rate limiting under load (2 replicas, write quota of 50 burst + 20 tokens/s, 20 s):
**445 transfers admitted** (the quota allows at most about 450), 7,475 rejected with 429.
The limit holds globally across replicas.

## Analysis

1. **Reads and independent writes scale, sub-linearly.**
   - Read-heavy throughput rose 2.4× with 4 replicas, and mixed transfers 3.0×.
   - Latency *fell*: p95 213 → 133 ms for reads, 740 → 443 ms for transfers. Each replica
     queues less work.
2. **The first limit is the replicas' CPU.**
   - Every API container ran at 110–145% of a core in every configuration.
   - All replicas, the database, NGINX and the load generator share 12 cores, so each added
     replica gets a smaller share of the machine.
   - On separate hosts the curve would be steeper. On one laptop, sub-linear is the expected
     and observed shape.
3. **The next limit is PostgreSQL, for writes.**
   - Mixed transfers at 4 replicas drove PostgreSQL to 121% CPU, up from 34% at 1 replica.
   - Every transfer is one transaction with row locks, five inserts (idempotency record, transfer, two postings, outbox) and three updates (two balances, the idempotency outcome), so
     write scaling will flatten next at the database.
   - That's the point to add connection pooling (PgBouncer), faster storage, and read
     replicas for history.
4. **A hot account does not scale. It plateaus.**
   - Throughput rose from 54 to 102 req/s with 2 replicas, then **stayed at 105 with 4**.
   - API CPU *dropped* from 94% to 37% while latency rose: the replicas were waiting, not
     working.
   - Every transfer must hold the same row lock, so the ceiling is roughly one transaction at
     a time, about 100 per second here, no matter how many replicas exist.
   - With one replica the hot workload is also CPU-bound and unstable (± 11 req/s, p99 4 s).
     With more replicas it becomes purely lock-bound and very steady (± 1–4 req/s).
5. **Correctness held under all load.**
   - 0.00% errors across 27 measured runs.
   - No lock timeouts: waits stayed under the 3 s `lock_timeout`. The p99 tails include
     queueing for a pooled connection.

## Trade-offs and what I would do next

| Bottleneck | Evidence | Next step |
|---|---|---|
| API CPU (Python, one machine) | 110–145% per replica at every scale | more hosts; profile serialization and validation; uvicorn workers per container |
| PostgreSQL CPU on writes | 34% → 121% from 1 to 4 replicas | PgBouncer, fewer round trips per transfer, read replicas for history and reconciliation |
| Single-row lock on a hot account | throughput plateau at ~105 req/s, API CPU 37% | sub-account sharding (split the balance across N rows), or batching debits per account |
| Load generator on the same machine | shares the 12 cores | dedicated load host for absolute numbers |

The earlier Phase 3 numbers ([phase-3-design.md](../phase-3-design.md#13-benchmarks)) were
single runs with 64 users, with the broker and workers running alongside. They are not
directly comparable to these.
