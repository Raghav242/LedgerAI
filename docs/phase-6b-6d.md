# Phase 6B–6D: Reliability, AI, Performance, CI/CD and Release Polish

Phase 6A ([phase-6a-hardening.md](phase-6a-hardening.md)) set the security baseline. This
phase finishes the release: it fixes the remaining reliability defects, repairs the anomaly
model's blind spot with an honest evaluation, measures performance repeatably, adds CI, and
packages the application for reproducible runs. Every result below was produced by the
commands named next to it, on the developer laptop (Windows 11, Docker Desktop, 12 CPUs /
7.6 GiB to the Docker VM), on 2026-11-23.

Contents: [Reliability and security](#6b-reliability-and-security) ·
[AI](#6b-ai-anomaly-detection) · [Performance](#6c-performance) · [CI/CD](#6c-cicd) ·
[Deployment and public demo](#deployment-and-public-demo) · [Polish](#6d-product-polish) ·
[Verification](#verification) · [Remaining risks](#remaining-risks)

---

## 6B: Reliability and security

### Per-consumer dead-letter queues

| | |
|---|---|
| **Defect** | Every main queue named one shared **fanout** DLX, so a message that failed in one consumer was copied into every consumer's DLQ. DLQ depth over-counted, and failures were attributed to consumers that never saw them. |
| **Constraint** | Queue arguments are immutable. Changing `x-dead-letter-exchange` means deleting queues that may hold messages. |
| **Fix** ([ADR 0007](adr/0007-per-consumer-dead-letters.md)) | Keep every queue and the fanout. The fanout feeds a new **direct** router; each DLQ is bound by its own name; an alternate exchange catches anything unattributable (`ledgerflow.events.unrouted.dlq`). Each consumer applies a policy that sets `dead-letter-routing-key` for its own queue, and publishes its own dead letters straight to its DLQ. The topology declaration removes the legacy fanout bindings idempotently. |
| **Dead end** | A headers exchange on `x-last-death-queue` was tried first. RabbitMQ's headers exchange ignores `x-` headers, so it matched everything. A throwaway probe also showed that `x-first-death-queue` names the retry tier, not the main queue. |
| **Verified** | 5 real-broker tests (two consumers on one prefix; poison via delivery limit; consumer-side rejects; migration from the legacy topology with existing dead letters; no-policy fallback; unattributable letters), stable over 3 consecutive runs. Live migration of the development broker: its one pre-existing dead letter was preserved, and the bindings and policies match the design. |

### Separate migration and runtime database roles

- The `migrate` job (schema owner) runs Alembic, then `python -m app.core.db_roles`. That
  creates `ledgerflow_app` with exactly the grants each table needs and verifies them with
  `has_table_privilege`. It **fails if any table has no policy**.
- Replicas, workers and retention connect as the runtime role. Ledger, transfer, receipt,
  audit and AI-result tables are **INSERT-only** for the application.
  ([ADR 0008](adr/0008-least-privilege-db-role.md),
  [database.md](architecture/database.md#3-roles-and-privileges-phase-6b))
- The whole integration suite now runs the application **as a runtime role**, so a missing
  grant fails a test. `test_db_roles.py` proves 15 forbidden operations are refused (UPDATE
  or DELETE on the ledger, TRUNCATE, CREATE TABLE, ALTER, DROP) and that privilege drift is
  detected and repaired.
- **Found on the way:** `test_migrations` rebuilds the schema mid-session, which drops all
  grants. The suite now re-applies them, exactly as the migrate job does after every
  upgrade.

### Stack tests in an isolated Compose project

- `pytest -m stack` now brings up `ledgerflow-stacktest`: the complete stack, with its own
  containers, network `172.30.0.0/24`, volumes, ports (gateway 127.0.0.1:8280) and
  database. It removes the project afterwards; the development stack keeps running.
- `assert_isolated()` checks the stack-test project against both dev and bench. A
  rendered-config unit test proves the three projects share no container, port, network,
  subnet or volume.
- **Found on the way:** one stack test still reset rate limits through a default argument
  pointing at the *development* Redis. `reset_rate_limits()` now requires an explicit URL,
  so that mistake can't recur silently.

### Retention

`python -m app.maintenance.retention [--dry-run]` (Compose profile `maintenance`) prunes
finished lab runs after 30 days (timelines cascade) and stale heartbeat rows after 7 days,
in bounded batches. It names no financial table, and the runtime role it runs as could not
delete one anyway. Tests cover dry run, idempotence, active-run protection and unchanged
reconciliation.

### Backup and restore

`python scripts/db_backup.py restore-drill`:
- `pg_dump --format=custom` taken inside an exported REPEATABLE READ snapshot, so the
  manifest's row counts match the dump under live writes;
- restored into a **throwaway** PostgreSQL container (no port, no volume);
- verified: row counts, sum of postings = 0, balances = postings, append-only triggers,
  Alembic version.

Drill on the development database: **20,010 rows in 12 tables, counts equal, ledger total 0,
0 mismatched accounts, 4 triggers, version 0006**. A stack test repeats it against the
stack-test database with workers running. Runbook: [operations.md](operations.md).

### Remaining security review

- **Least privilege per process:**
  - API: runtime DB role, monitoring-only broker user, no admin passwords.
  - Workers: runtime DB role and broker admin (needed to declare topology and apply their
    own policy); no lab key, no monitoring password.
  - Migrate: owner. Frontend container: no credentials at all.
- **Frontend image:** unprivileged NGINX; CSP, `nosniff`, `frame-ancestors 'none'`,
  `no-referrer`; `/controller` returns 404.
- **Secret scanning:** gitleaks over the full history found one false positive (a
  routing-key variable). It is allow-listed with a reason; the history scan is now clean.
  The working tree's real secrets live only in the gitignored `.env`.
- **Dependencies:** pip-audit (3 lock files) and npm audit (production and all) report no
  known vulnerabilities.

---

## 6B: AI anomaly detection

### Why v1 never flagged a burst

From the held-out test split:
- **Burst transfers are separable.** Gap to the previous transfer < 0.57 s at the 95th
  percentile; only 0.04% of normal transfers have a gap < 1 s.
- **v1 scored them below threshold anyway.** Burst scores peaked at 0.580 against a
  threshold of 0.622.
- **Dilution.** An Isolation Forest picks a random feature per split, so one extreme
  feature out of six barely shortens the isolation path.
- **Absolute velocity feature.** `burst_60s` is absolute: fast normal accounts also reach
  11 transfers per minute.
- **Threshold choice.** The F1-optimal threshold was dominated by 334 large-amount and 12
  new-account anomalies versus 49 bursts.

### What changed (iforest-v2)

- **Feature schema v2.** Three velocity features relative to the account's **own** cadence,
  from transfers strictly before the scored one:
  - `gap_vs_typical`
  - `burst_10s`
  - `rate_vs_typical_60s`

  Leakage tests append future events and check nothing changes. An integration test proves
  the worker's SQL statistics equal the training replay.
- **One forest per feature family** (amount/history, velocity), combined by the maximum
  training percentile ([app/ai/ensemble.py](../app/ai/ensemble.py)).
  - A single 9-feature forest was tried first and measured: burst recall 0.82, but large
    recall fell from 0.78 to 0.45, the same dilution in reverse.
- **Deployed decision: model OR rule baseline.** Adopted because its *validation* F1 (0.985)
  beat the model alone (0.603) by ≥ 0.01. No threshold is tuned on test data.
- **Engineering:**
  - versioned registry (`iforest-v1` kept and loadable);
  - SHA-256 and scikit-learn version verified before unpickling;
  - feature-schema validation (invalid inputs are dead-lettered, never scored);
  - **byte-for-byte reproducible training** (`python -m app.ai.train` in a fresh process
    rebuilds the shipped artifact; v1 too);
  - `/api/v1/ai/drift` (PSI against training deciles);
  - an evaluation report (`python -m app.ai.evaluate` → `app/ai/model/evaluation.json`).

### Evaluation (synthetic data only; thresholds from validation)

Held-out accounts (25,465 transfers; 334 large, 49 burst, 12 new-account anomalies):

| Detector | Precision | Recall | F1 | FPR | Large | Burst | New account |
|---|---|---|---|---|---|---|---|
| v1 model (Phase 5E) | 0.766 | 0.689 | 0.725 | 0.33% | 0.778 | **0.000** | 1.000 |
| Rule baseline | 0.972 | 0.982 | 0.977 | 0.04% | 0.985 | 1.000 | 0.833 |
| v2 model alone | 0.497 | 0.759 | 0.601 | 1.21% | 0.716 | **1.000** | 1.000 |
| **v2 hybrid (deployed)** | 0.970 | **0.985** | **0.977** | 0.05% | 0.985 | 1.000 | **0.917** |

Three entirely new worlds (seeds 101/202/303, every account unseen), F1 mean ± std:
v1 0.717 ± 0.012 · rules 0.979 ± 0.001 · v2 model 0.626 ± 0.015 · **v2 hybrid 0.978 ± 0.001**.
Burst recall (mean): v1 0.000 · v2 model 0.987.

Distribution shifts never seen in training (seed 404):

| Shift | v1 F1 · burst | Rules F1 · burst | v2 model F1 · burst | v2 hybrid F1 · burst |
|---|---|---|---|---|
| amounts × 3 | 0.701 · 0.007 | 0.961 · 1.000 | 0.682 · 0.993 | 0.957 · 1.000 |
| normal accounts 2–4× faster | 0.814 · 0.010 | 0.986 · 1.000 | 0.717 · 0.995 | 0.985 · 1.000 |
| bursts slowed to 0.6–2 s | 0.715 · 0.000 | 0.925 · **0.296** | 0.618 · **0.875** | 0.935 · 0.441 |

Single-row CPU inference: v1 p50 7.9 ms, v2 p50 16.2 ms (two forests; asynchronous, never on
the request path).

**Honest reading:**
- The rule baseline is near an upper bound in-distribution, because its rules restate how
  the synthetic generator creates anomalies.
- The unsupervised model pays about 1.2% false positives.
- Its value shows when the world shifts. The cadence features still catch 88% of slowed
  bursts, where a fixed "< 1 s" rule catches 30%.
- The deployed hybrid's threshold was set where validation said, which is strict, so it
  inherits only part of that robustness (0.44).
- Lowering it would need labelled shifted data to justify. Tuning it on these test worlds
  would be cheating.
- Nothing here measures real-world fraud.

The AI Lab shows all of this: the comparison table, fresh-seed spread, shift table,
model-vs-rules disagreements, the model's and the rules' verdicts separately for each
scored transfer, live drift, and the limitations.

---

## 6C: Performance

Isolated `ledgerflow-bench` project; Locust 2.47 in a container on the same laptop; 32
closed-loop users; for each (replicas, workload) a 10 s warmup, then 3 measured runs of
20 s; mean ± standard deviation (`benchmarks/results/20261123-182416.json`). New: a
**hot-account** contention workload, warmup, repeats, memory sampling, scaling efficiency,
and lock-timeout counting.

| Workload | 1 replica | 2 replicas | 4 replicas | p95 at 1 → 4 | Errors |
|---|---|---|---|---|---|
| Read-heavy | 252 ± 5 req/s | 361 ± 45 req/s (72%) | 598 ± 26 req/s (59%) | 213 → 133 ms | 0.00% |
| Mixed transfers | 68 ± 7 req/s | 118 ± 6 req/s (86%) | 205 ± 5 req/s (75%) | 740 → 443 ms | 0.00% |
| Hot account (contention) | 54 ± 11 req/s | 102 ± 1 req/s (93%) | 105 ± 4 req/s (48%) | 1333 → 1067 ms | 0.00% |

- **Reads and independent writes scale sub-linearly**: 2.4× (reads) and 3.0× (mixed) with 4
  replicas, and p95 latency *falls*. Each API container ran at 110–145% of a core in every
  configuration: on one shared 12-core laptop, replica CPU is the first limit.
- **PostgreSQL is next for writes**: 34% → 121% CPU from 1 to 4 replicas on mixed transfers.
- **A hot account plateaus**: 54 → 102 → 105 req/s, while API CPU *drops* to 37%. One row
  lock serializes every transfer (~100 per second here), regardless of replica count.
- 0.00% errors in all 27 measured runs; no lock timeouts; the global write quota admitted
  445 of a ~450 allowance under load.
- **Discarded run**: the first full run overlapped a 2-minute secret scan during its
  4-replica stage; it was moved out of `benchmarks/results` and rerun on an idle machine.

Full table and analysis: [case study 3](case-studies/03-scaling-performance.md). The
Performance Lab shows these numbers with their spread and derived insights only.

---

## 6C: CI/CD

| Workflow | Jobs | Notes |
|---|---|---|
| `.github/workflows/ci.yml` (push, PR) | backend (ruff, format, unit tests incl. model reproducibility and Compose isolation); integration (PostgreSQL 16, Redis 7.4, RabbitMQ 4.1 service containers; integration + rate-limit suites as the runtime role); migrations (`alembic upgrade head`, `alembic check`, downgrade/upgrade round trip, DB-role apply + verify); frontend (npm ci, lint, typecheck, Vitest, build); OpenAPI (types regenerated from `scripts/export_openapi.py`, `git diff --exit-code`); compose (dev/profile/demo configs render; API, AI and frontend images build) | `permissions: contents: read`; actions pinned to commit SHAs; `persist-credentials: false`; throwaway CI credentials; pinned `requirements-dev.lock` |
| `.github/workflows/security.yml` (push, PR, weekly) | pip-audit on every lock file; npm audit (production fails on high, dev reported); gitleaks over full history; Trivy image scan (HIGH/CRITICAL with a fix) and config scan | scanner images pinned by digest |
| `.github/workflows/stack.yml` (manual) | `pytest -m stack` in the isolated stack-test project | destructive experiments (failure lab, benchmarks) are never run in CI |

**Validated locally:** actionlint (see Verification), and every command the workflows run.
**Not run:** the hosted workflows themselves, because nothing was pushed. Their first
hosted run is unverified.

---

## Deployment and public demo

- `docker compose up -d --build --wait` now includes:
  - the **frontend** (production build on unprivileged NGINX at 127.0.0.1:3000, same-origin
    proxy to the gateway);
  - migrations plus DB roles;
  - workers and the AI model;
  - optional observability (`--profile observability`).
- Dependencies: `requirements.lock`, `requirements-ai.lock`, the new `requirements-dev.lock`
  and `package-lock.json`. Base and scanner images are pinned by tag in Compose and by
  digest in CI.
- **Public demo design:** [public-demo.md](public-demo.md) and `docker-compose.demo.yml`.
  - Production mode: the app refuses any development aid.
  - No data-store ports.
  - Strict quotas.
  - No controller.

  **Verified** as a separate Compose project (`ledgerflow-demo`, its own network, ports and
  volumes; removed afterwards):
  - `up --wait` exit 0.
  - Account creation 201; reconciliation 200.
  - `/fund`, `/lab/*`, `/observability/*`, `/ai/*` all 404; no `X-Served-By`.
  - 8 rapid writes → `201 201 201 201 429 429 429 429`.
  - Only NGINX and the frontend published, on 127.0.0.1; PostgreSQL, Redis and RabbitMQ
    published 0 ports.
  - API: `APP_ENV=production` as `ledgerflow_app`.

  The built API image refuses `ENABLE_DEV_ENDPOINTS=true` both with `APP_ENV=production` and
  with `GATEWAY_BIND=0.0.0.0` (exit 1, with the reason).

---

## 6D: Product polish

All existing pages and flows are kept: Experiment Lab, request visualizer, event pipeline,
failure lab, AI Lab and guided demo. Changes:

| Area | Change | Why |
|---|---|---|
| Navigation | ten flat links → four labelled groups (Start, Money, Labs, Observe) with `role="group"` | scannable hierarchy; screen readers announce the section |
| Accessibility | "Skip to main content" link (first Tab stop) and a focusable `<main id="main">`; per-page `document.title` ("Ledger & Reconciliation · LedgerFlow AI") | keyboard users skip the sidebar; tabs and history name the page |
| AI Lab | model card rebuilt around the evaluation report: deployed version/decision/threshold first; held-out comparison table (best value per row in bold); fresh-seed spread, shift table, disagreements, live drift and findings behind disclosures; each verdict shows the model's and the rules' calls separately, with a "disagree" marker; version-aware score scale (v2 percentiles shown as "more unusual than X % of training transfers") | progressive disclosure; never present the hybrid's verdict as the model's alone |
| AI Lab (bug) | the run summary printed "score ≥ threshold" whenever the hybrid flagged, even when only the rules fired; it now states the model's own comparison and the rules' verdict | a displayed result must be true |
| Performance Lab | hot-account workload; ± standard deviation over repeats in the tables; insights for run-to-run spread and for lock-bound contention | variability and bottlenecks shown, not hidden |
| Frontend image | production build on unprivileged NGINX with CSP, `nosniff`, `X-Frame-Options DENY`, `no-referrer`; `/controller` is 404 | the containerized UI is safe to expose with the demo profile |

**Visual review** (all 10 pages screenshotted at 1440 px and 390 px against the real stack):
- **No horizontal overflow on any page at either width.** Every page has its own `<h1>` and
  tab title.
- **Found and fixed:**
  1. The Performance Lab said "the replicas' CPU is the measured limit" for mixed transfers
     at 4 replicas, although PostgreSQL (121%) was busier than any API container. The
     bottleneck statement now names the busiest component and says when the database is
     becoming the limit; for the hot account it says the replicas are mostly idle, waiting
     for the row lock.
  2. "What did we learn?" had grown to 19 lines. Latency, connection and spread statements
     now sit behind a disclosure, and the duplicated "sub-linear" warning for the hot account
     is gone.
  3. False-positive rates rounded to "0.000" vs "0.012" in the AI Lab; they are now shown as
     percentages (0.04% vs 1.21%).

  Each fix has a regression test (the insights test runs against the real measured result
  file).

Documentation added: [HLD](architecture/hld.md), [LLD with sequence diagrams](architecture/lld.md),
[database design](architecture/database.md), [12 ADRs](adr/README.md),
[operations runbook](operations.md), [public demo](public-demo.md), three case studies
([correctness](case-studies/01-financial-correctness.md),
[reliability](case-studies/02-event-reliability.md),
[scaling](case-studies/03-scaling-performance.md)) and the
[interview guide](interview-guide.md).

---

## Verification

All executed on 2026-11-23 on the developer laptop; nothing below was skipped unless stated.

| Check | Result |
|---|---|
| `ruff check .` / `ruff format --check .` | clean (218 files) |
| `pytest` (unit + integration + Redis + RabbitMQ; app runs as the runtime DB role) | **503 passed**, 12 stack tests deselected (6A baseline: 449) |
| `pytest tests/unit` after the final image/Dockerfile changes | 257 passed |
| `pytest -m stack` (isolated `ledgerflow-stacktest`, final images, NGINX 1.30) | **12 passed**; project and volumes removed afterwards |
| Real-broker DLQ routing tests | 5/5, three consecutive runs |
| Frontend `lint` / `typecheck` / Vitest / `build` | clean / 0 errors / **194 passed** / built |
| Playwright regular (real stack, Edge) | **14 passed**, 5 destructive skipped |
| Playwright destructive (`E2E_FAILURES=1`, `python -m labctl --enable-failures`) | **5 passed**; journal cleared, all services healthy, DLQ policies re-applied, ledger reconciles |
| Opt-in benchmark Playwright test | not run (it writes into `benchmarks/results`; the harness was run directly instead) |
| AI: `python -m app.ai.train` (fresh process) | rebuilds the shipped v1 and v2 artifacts byte for byte |
| AI: worker SQL features = training replay; async scoring via the real stack | integration test passes; AI scenarios pass in Playwright |
| Benchmarks | 27 measured runs + quota run, 0.00% errors; bench project removed; one contaminated run discarded and rerun |
| Backup/restore drill (dev database) | 20,010 rows, counts equal, ledger total 0, 0 mismatched accounts |
| `docker compose up -d --build --wait` | exit 0; all services healthy incl. frontend (uid 101) |
| Public-demo profile (`ledgerflow-demo`) | see [above](#deployment-and-public-demo) |
| `alembic check` | "No new upgrade operations detected" |
| OpenAPI drift (`export_openapi.py` → `openapi-typescript` → diff) | identical to the committed types |
| actionlint (3 workflows) | clean |
| pip-audit (3 lock files) / npm audit (prod + all) | no known vulnerabilities / 0 |
| gitleaks: git history / working tree | no leaks (after allow-listing one false positive) / only the gitignored `.env` and third-party `.venv` |
| Trivy, our images (HIGH/CRITICAL with a fix) | **api, ai, frontend: 0.** First scan found 1 (zlib) in api/ai, 5 more from pip's vendored libraries, and 91, then 6, in the frontend's NGINX base. Fixed by OS updates at build time, removing pip from runtime images, and moving to the official `nginx:1.30-alpine`, run as non-root. The gateway also moved from `nginx:1.28-alpine` (85 findings) to 1.30 (0). |
| Trivy, third-party images | redis, rabbitmq, nginx 1.30: 0. `postgres:16-alpine`: 1 Alpine HIGH + 26 in its bundled `gosu` Go binary (upstream; accepted, see risks) |
| Hosted GitHub Actions runs | **not run** (nothing pushed) |

**Environment note:** during verification another local project started a PostgreSQL on
127.0.0.1:5432. LedgerFlow's PostgreSQL was published on 5433 for the remaining checks
(`POSTGRES_PORT=5433` as an environment override; `.env` unchanged). Development data was
verified intact afterwards (row counts identical, ledger reconciles).

---

## Remaining risks

| Risk | Impact | Next step |
|---|---|---|
| No authentication; one global quota | not deployable publicly; one client can starve others | principal from verified credentials (the seam exists), per-principal quotas |
| Single PostgreSQL primary, single Redis, single RabbitMQ node | node or disk loss = outage (RabbitMQ: unconsumed messages) | managed PostgreSQL with replicas, 3-node RabbitMQ (quorum queues ready) |
| Hot accounts serialize | throughput per account bounded by one transaction | sub-account sharding or debit batching |
| Idempotency records and outbox history never expire | unbounded growth | expiry after a documented client retry window; archive published outbox rows |
| AI evaluated on synthetic data only; hybrid threshold strict under shift | says nothing about real fraud | labelled data, shadow deployment, threshold review with shifted labelled sets |
| Fixed container IPs | local workaround only | service discovery |
| Lock files pin versions, not hashes | weaker supply-chain guarantee | `pip-compile --generate-hashes` |
| Hosted CI never run | first run may need fixes | push to a branch and run the workflows |
| Benchmarks share one laptop with the load generator | absolute numbers are machine-specific | dedicated load-generation host |
| `postgres:16-alpine` ships a `gosu` binary built with an old Go toolchain (26 Trivy findings) + 1 Alpine HIGH | upstream image; gosu runs only at container start to drop privileges | track upstream releases, or build PostgreSQL on a patched base |
