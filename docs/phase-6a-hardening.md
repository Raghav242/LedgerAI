# Phase 6A: Security, Reliability and Release Baseline

Phase 6A adds no product features. It closes the security and isolation gaps found in a
pre-release review, adds tests that fail if those gaps reopen, and writes down exactly what
the system does and does not protect against. The model was not retrained and the
frontend was not redesigned. The only UI change is one corrected sentence in the
Performance Lab's confirmation dialog.

Contents:

1. [Security architecture](#1-security-architecture)
2. [Threat model and remaining risks](#2-threat-model-and-remaining-risks)
3. [Configuration and secret management](#3-configuration-and-secret-management)
4. [Failure and recovery behaviour](#4-failure-and-recovery-behaviour)
5. [Infrastructure isolation (benchmarks)](#5-infrastructure-isolation-benchmarks)
6. [Verification results](#6-verification-results)
7. [Known limitations](#7-known-limitations)
8. [Required work before public deployment](#8-required-work-before-public-deployment)

---

## 1. Security architecture

LedgerFlow is a **local development and demonstration system**. It has no user
authentication. Every request acts as one fixed, server-defined demo principal. The
security design therefore works in layers. First, nothing is reachable from outside the
machine. Second, each surface that is dangerous by nature can be switched off and is
refused in any configuration that could be exposed. Third, each process holds only the
credentials it needs.

```
 browser ──▶ Vite dev server (127.0.0.1:5173) ──▶ NGINX (127.0.0.1:8080, only published API port)
                    │                                   │ internal network 172.28.0.0/24
                    │ adds controller token             ▼
                    ▼                          api-1, api-2 (no published ports)
          labctl (127.0.0.1:8090,                │  PostgreSQL, Redis (loopback ports only)
          host process, Docker CLI)              │  RabbitMQ mgmt API: monitoring user only
                                                 ▼
                          outbox-publisher, event-consumer, ai-worker (broker admin creds)
```

| Surface | Protection |
|---|---|
| Public API (`/api/v1/accounts`, `/transfers`, reads) | Loopback-only gateway by default; global Redis token-bucket rate limits (writes fail closed); bounded inputs (body size 64 KiB at NGINX, query limits, header lengths); idempotency scope set by the server |
| Development surface: `/fund`, `/lab/*`, `/observability/*`, `/ai/*`, `/performance/*` | Mounted only with `ENABLE_DEV_ENDPOINTS=true`, which is **refused** when `APP_ENV=production` and, new in 6A, when the gateway is published on a non-loopback address (`GATEWAY_BIND`, set from `NGINX_BIND`) or when `LAB_TRAFFIC_SECRET` is missing or weak |
| Lab traffic marker `X-Lab-Traffic` | HMAC-SHA256 with an **independent** secret (6A: the fallback derived from `POSTGRES_PASSWORD` is removed); bound to run id and rate mode; forged or invalid tokens are ignored, and the request is then treated as an ordinary demo-merchant request |
| Experiment controller (`labctl`) | Host process bound to `127.0.0.1` (not configurable); per-start random token injected by the Vite proxy (never sent to the browser); Host and Origin checks (DNS-rebinding defence); no CORS; refuses `APP_ENV=production`, a non-local database, or (6A) a missing lab secret; four allow-listed Compose services with fixed argument lists, journaled and restored |
| Docker | No container mounts the Docker socket. Only the host-side controller and scripts use the Docker CLI, through allow-lists |
| Broker | API replicas hold **no** broker admin credentials (6A); they read health checks with a `monitoring`-tagged user whose permissions are `^$` (match nothing) |
| Proxy headers | uvicorn trusts `X-Forwarded-*` only from NGINX's fixed address (`FORWARDED_ALLOW_IPS`), no longer from `*` (6A) |
| Model artifact | Pickle loaded only after a SHA-256 match with `metadata.json` and an exact scikit-learn version match (6A) |
| Images | Non-root `app` user, slim base, no secrets baked in (`.dockerignore` excludes `.env`, `labctl`, `.ledgerflow`, tests) |

### What the lab signature is, and what it is not

`X-Lab-Traffic` is a **server-to-server marker**. Only the lab runner holds the key: it
runs inside a replica or the local controller. The runner attaches the token to the
simulated requests it sends through NGINX. A replica that verifies the token gives the run
its own rate-limit bucket and idempotency namespace. That is all the token does. It
grants no extra privilege and identifies no user. It is **not authentication**, and it must
not be used as such. A request without a valid token is an ordinary request, with the
ordinary limits.

The earlier key derivation, `HMAC(POSTGRES_PASSWORD, label)`, let anyone who knew the
database password forge lab tokens, and the development default of that password is public
in `.env.example`. A forger could escape the shared quota or use the burst policy. The key
now comes only from `LAB_TRAFFIC_SECRET`, which must be at least 32 characters with at
least 10 distinct characters, and must differ from every database and broker password. The
check runs in the `Settings` validator *and* again in `LabTrafficSigner.from_settings`,
because `Settings.model_copy` skips validators. A regression test shows that a token
signed with the old derived key is rejected.

---

## 2. Threat model and remaining risks

**In scope (local development):** a malicious web page in the developer's browser; other
devices on the same LAN; a mistaken configuration (wrong `.env`, `NGINX_BIND=0.0.0.0`,
`APP_ENV=production` with development settings); a tampered or stale model file; tooling
that writes to the wrong database.

| Threat | Mitigation | Residual risk |
|---|---|---|
| LAN device calls `/fund` or starts lab load | Gateway on loopback; dev surface refused unless `GATEWAY_BIND` is loopback | An operator who edits `GATEWAY_BIND` directly, out of step with `NGINX_BIND`, defeats the check. Both come from one variable in Compose |
| Web page drives the controller (CSRF, DNS rebinding) | Token in a file, never in the browser; Host/Origin checks; no CORS | Any local process running as the same OS user can read `.ledgerflow/controller-token` |
| Forged lab token escapes rate limit | Independent HMAC key; constant-time compare | The key is shared by every replica and the controller; leaking `.env` leaks it |
| Client spoofs `X-Forwarded-For` | Trusted only from NGINX's address | NGINX appends to, and does not replace, an incoming `X-Forwarded-For` (no decision currently depends on it) |
| API replica compromised | No broker admin creds; DB user is still the schema owner | **The application's DB user owns the schema** (DDL rights). A least-privilege runtime DB role is Phase 6B |
| Tampered model file runs code on unpickle | SHA-256 match plus exact scikit-learn version check before `joblib.load` | Digest and artifact sit in the same repository, so this is an integrity check, not a signature |
| Benchmark corrupts dev data | Separate Compose project, its own PostgreSQL server; isolation asserted before any Docker call | Both stacks share CPUs and memory; that affects measurements, not correctness |
| Resource exhaustion | Rate limits; bounded pagination and parameters; bounded lab and benchmark params; 64 KiB bodies; pool caps | Reconciliation scans the whole ledger (rate-limited as a read); unauthenticated callers share one quota, so one caller can starve the others |
| Secrets in logs or bundles | `SecretStr`, URL masking, `hide_password=True`; no `VITE_*` secrets; the controller token is added by the dev proxy | `DB_ECHO=true` logs bound parameters (refused in production) |
| Dependency vulnerabilities | Exact pins; `pip-audit` and `npm audit` clean at 6A | Pins are not hash-locked; no automated audit in CI (no CI exists) |

**Out of scope for a local system:** multi-tenant isolation, an attacker with code
execution as the developer's OS user, TLS on loopback, and DoS by a party that can reach
the gateway.

---

## 3. Configuration and secret management

| Secret | Who holds it | Where it comes from |
|---|---|---|
| `POSTGRES_PASSWORD` | postgres, migrate, API replicas, workers, host tools | `.env` (dev default is public, non-secret) |
| `RABBITMQ_PASSWORD` (broker admin) | rabbitmq, workers, `rabbitmq-monitor-setup` | `.env` (dev default is public, non-secret) |
| `LAB_TRAFFIC_SECRET` | API replicas, `labctl` (host) | `.env`, **generated per machine**; workers explicitly get `""` |
| `RABBITMQ_MONITOR_PASSWORD` | API replicas (probe), `rabbitmq-monitor-setup` | `.env`, generated per machine; workers explicitly get `""` |
| Controller token | `labctl`, Vite dev server | Random per controller start, `.ledgerflow/controller-token` (gitignored) |

Rules the code enforces:

- `.env`, `.env.*`, `*.pem`, `*.key`, `secrets/` and `.ledgerflow/` are gitignored, and
  `.dockerignore` keeps them out of every image.
- `docker compose` fails fast (`${VAR:?}`) when the PostgreSQL or RabbitMQ credentials are
  missing.
- An empty variable counts as unset (`VAR: ${VAR:-}` in Compose produces `""`). It never
  becomes an empty key.
- Production (`APP_ENV=production`) refuses: dev endpoints, `EXPOSE_REPLICA_HEADER=true`
  (6A), `DB_ECHO=true` (6A), `RATE_LIMIT_BACKEND=memory`, and consumer fault injection.
- The workers get `ENABLE_DEV_ENDPOINTS=false`, no lab key and no monitoring password (6A,
  least privilege).

**The RabbitMQ monitoring user.** The one-shot `rabbitmq-monitor-setup` job
(`python -m app.events.broker_monitor_setup`) uses the admin credentials to create or update
`RABBITMQ_MONITOR_USER` with:

| Grant | Value | Why |
|---|---|---|
| tags | `monitoring` only | Management API: health checks, queue depths |
| events vhost permissions | configure `^$`, write `^$`, read `^$` | RabbitMQ 4.x shows a monitoring user only the queues of vhosts it can access. `^$` matches no resource name, so the user cannot declare (not even server-named queues), publish, consume, purge or delete |
| other vhosts, topic permissions | none | Removed on every run |

The job verifies the end state, refuses to reuse the admin user name or password, and does
nothing when `RABBITMQ_MONITOR_PASSWORD` is unset. Nothing depends on the job's success
(`required: false`), so a failed monitoring setup can never stop event delivery or the
API. `docker compose up` still reports the failure.

**Development, test and benchmark configurations stay distinct:**

| | Development | Integration tests | Stack tests | Benchmarks |
|---|---|---|---|---|
| Compose project | `ledgerflow` | none (host process) | `ledgerflow` (redeployed) | `ledgerflow-bench` |
| PostgreSQL server | dev container, volume `ledgerflow_pgdata` | dev container | dev container | **own container, own volume** |
| Database | `ledgerflow` | `ledgerflow_test` (name must end in `_test`) | `ledgerflow_stacktest` | `ledgerflow_bench` |
| Gateway | 127.0.0.1:8080 | TestClient | 127.0.0.1:8080 | 127.0.0.1:8180 |
| Redis | dev, db 0 | db 15, unique key prefixes | dev, db 0 | own container (127.0.0.1:6479) |
| RabbitMQ | dev | dev broker, unique `test-<random>` prefix | dev | none |

---

## 4. Failure and recovery behaviour

| Failure | Behaviour | Evidence |
|---|---|---|
| **Redis down** | Writes `503 RATE_LIMITER_UNAVAILABLE` within the 250 ms connect timeout (fail closed); reads fail open; `/health/ready` returns 503 with `redis: unavailable`; liveness unaffected. Fixed addresses in `/etc/hosts` avoid a 7 s DNS stall | `demo_redis_failure.py`, failure lab, `test_rate_limit_http.py` |
| **Redis restart** | Buckets start full: at most one burst of over-admission. No financial data lives in Redis | stack test (6A) |
| **PostgreSQL restart** | Requests during the restart fail with 503; pooled connections reconnect (`pool_pre_ping`); data persists in the named volume; idempotency records survive, so a retried transfer *replays* | `test_restarts_preserve_committed_money_and_idempotency` (6A) |
| **Replica down** | NGINX skips it after one failed connect (`max_fails=1 fail_timeout=2s`, 500 ms connect timeout); POSTs are never re-sent to another replica after transmission (the client retries with the same Idempotency-Key) | `test_multi_replica.py`, failure lab |
| **Replica shutdown** | uvicorn `--timeout-graceful-shutdown 10`; Compose `stop_grace_period: 15s` (6A) so Docker doesn't SIGKILL in-flight requests at 10 s | config |
| **RabbitMQ down** | Transfers keep committing; events wait in the outbox; publisher backs off; consumers reconnect; topology probe reports `unhealthy` from the management API (6A) or `derived` from worker heartbeats | `demo_outbox_recovery.py`, `test_broker_outage.py`, unit probe tests |
| **RabbitMQ resource alarm** | Probe reports `degraded` ("publishers are blocked; events wait in the outbox") | unit probe tests |
| **Publisher crash mid-batch** | Claim lease expires (30 s) and another publisher re-claims; at-least-once with consumer dedup | `test_crash_recovery.py` |
| **Consumer crash before/after commit** | Redelivery; dedup by event id; effect exactly once; poison messages dead-lettered after the delivery limit | `demo_worker_crash.py`, `test_event_delivery.py` |
| **DLQ** | Depth per queue and a "dead-lettered (all DLQs)" total in the live topology from the independent probe (6A), even when every worker is down | unit and real-broker tests |
| **Worker hang** | Heartbeat row goes stale past its announced deadline; container healthcheck on a heartbeat file (stale after 30 s) gives `unhealthy` | `test_observability.py` |
| **Controller killed mid-experiment** | Journal written before every disturbance; restored at the next start; run marked as interrupted | `test_lab_controller.py` |
| **Benchmark killed by time limit** | Controller removes the `ledgerflow-bench` project; the dev stack was never touched | unit test (6A) |
| **Migrations** | Run exactly once per `up` by the `migrate` job before any replica starts (`service_completed_successfully`); replicas never migrate | `test_migrations.py`; `alembic upgrade head` reported "No new upgrade operations detected" against the dev database |

**Timeout budget:** Redis 250 ms connect and socket; DB connect 5 s, pool wait 10 s
(replicas), row lock 3 s; NGINX connect 500 ms, read 30 s, total retry window 5 s; broker
connect 5 s, heartbeat 30 s, blocked-connection 30 s; topology probes 0.5 s connect and
1.5 s total.

**Connection budget:** 4 replicas × (5 + 5) + 3 workers × (2 + 2) + the lab controller ≈ 55,
below `max_connections=100`.

---

## 5. Infrastructure isolation (benchmarks)

**Before 6A:** `run_benchmarks.py` redeployed the **development** stack with
`API_POSTGRES_DB=ledgerflow_bench`. During a run, the dev UI, the event workers and the
`migrate` job all pointed at the benchmark database. Dev RabbitMQ queues (and messages
already in flight) were consumed against the wrong database. `reset_rate_limits()` wiped the
dev rate-limit buckets. A failed restore left the dev stack on the benchmark database.

**After 6A:** benchmarks run in a separate Compose project from the same
`docker-compose.yml`:

| | Dev (`ledgerflow`) | Benchmark (`ledgerflow-bench`) |
|---|---|---|
| Containers | `ledgerflow-*`, `ledgerflow-<svc>-N` | `ledgerflow-bench-*`, `ledgerflow-bench-<svc>-N` (`STACK_CONTAINER_PREFIX`) |
| Network | `ledgerflow_backend`, 172.28.0.0/24 | `ledgerflow-bench_backend`, 172.29.0.0/24 (`LEDGERFLOW_BENCH_NET`) |
| Volumes | `ledgerflow_pgdata`, `ledgerflow_rabbitmqdata` | `ledgerflow-bench_*` (removed after each run) |
| Ports (loopback) | 8080, 5432, 6379, 5672, 15672 | 8180, 5532, 6479, (5772, 15772 unused) |
| Database | `ledgerflow` | `ledgerflow_bench` on its own server |
| Services | everything | nginx, api-1..N, migrate, postgres, redis |

Guarantees in code (`scripts/stack_control.py`):

- `assert_isolated(BENCH)` runs before any Docker call. It refuses if the project name,
  container prefix, gateway URL, Redis URL, network or any published port equals the dev
  stack's, if the API database is not a dedicated one, or if the gateway isn't on loopback.
- `compose_env()` applies the project's isolation variables **last**, after all overrides,
  and removes `COMPOSE_PROJECT_NAME`, so nothing passed in can point the run back at dev.
- `teardown()` removes the project together with its volumes, and refuses the dev project.
- CPU sampling counts only `ledgerflow-bench-*` containers.
- The controller's timeout cleanup tears down the benchmark project. It no longer
  "restores" the dev stack, because the dev stack was never changed.

**Laptop cost:** the benchmark project starts 2 to 4 replicas, NGINX, PostgreSQL and Redis
(no broker or workers). Results recorded before 6A had the broker and workers running on
the same machine, so new numbers are not strictly comparable to old ones (the result
metadata records `compose_project`). If memory is tight, `docker compose stop` the dev
stack first. Its data stays in the volumes.

**Still on the dev project:** stack tests (`pytest -m stack`) and the failure demos
deliberately act on the development stack (stack tests use their own database). Moving
stack tests to an isolated project is a 6B item.

---

## 6. Verification results

All commands below were run on the developer laptop (Windows 11, Docker Desktop, Compose
v2.39.1) on 2026-11-23. The existing development volumes were used throughout and never
removed.

| Check | Result |
|---|---|
| `ruff check .` / `ruff format --check .` | clean |
| `pytest` (unit + integration + Redis + RabbitMQ, stack excluded) | **449 passed**, 7 deselected (was 383; +66 new) |
| `pytest tests/unit/test_hardening.py` | 61 passed, including the rendered-Compose comparison of dev and bench (no shared container, port, network, subnet or volume) |
| `pytest tests/integration/test_broker_monitor.py` (real broker) | 5 passed: the monitoring user reads health and queues; it is refused (403) for declare, server-named queue, publish and consume; it cannot list users |
| `pytest -m stack` | **10 passed** (7 existing + 3 new: restart recovery with idempotent replay and reconciliation; runtime secrets per container; broker health from the probe). Dev row counts and reconciliation unchanged afterwards |
| Frontend `npm run lint` / `typecheck` / `test` / `build` | clean / clean / **191 passed** / built (no secrets found in `dist/`) |
| Playwright (regular, real stack) | **14 passed**, 5 destructive skipped. One assertion was updated: RabbitMQ evidence is now `Probed` (or `Derived` without a monitor password) |
| Playwright destructive (`E2E_FAILURES=1`, `python -m labctl --enable-failures`) | **5 passed**; every disturbed service was restored, the journal was cleared, and the ledger reconciled afterwards (374 accounts, total 0). The opt-in benchmark e2e was not run, because it writes into the tracked `benchmarks/results/`. The harness was verified directly instead (below) |
| `pip-audit -r requirements.lock`, `-r requirements-ai.lock` | No known vulnerabilities |
| `npm audit` (all and `--omit=dev`) | 0 vulnerabilities |
| `docker compose up -d --build --wait` | exit 0; `migrate` and `rabbitmq-monitor-setup` exited 0; all services healthy; AI worker logged `Loaded model iforest-v1` after the digest check |
| Forced monitor-setup failure | `up --wait` exits 1 (reported), outbox publisher keeps running |
| Container secrets | API replicas: no `RABBITMQ_PASSWORD`; workers: empty `LAB_TRAFFIC_SECRET` / `RABBITMQ_MONITOR_PASSWORD`, `ENABLE_DEV_ENDPOINTS=false` |
| Live topology | RabbitMQ `healthy`, evidence `probe`, workers connected 3/3, per-queue depths, and 1 pre-existing dead-lettered message in `ledgerflow.ledger-audit.dlq` (left untouched) |
| **Benchmark isolation run** (`--duration 10 --users 8 --replicas 1 2 --skip-quota-run`, results written outside the repo) | harness exit 0. Dev stack snapshot before and after was **identical**: row counts (332 accounts, 1,809 transfers, 3,618 ledger entries, 1,671 outbox events, 4,570 idempotency records, 50 lab runs, 1,670 audit rows, 304 anomaly results), reconciliation `ok` (total 0), Alembic `0006`, and every dev container's id and start time. 104 dev `/health/ready` samples during the run, all 200. `ledgerflow-bench` left no container, volume or network |
| Benchmark numbers (8 users, 10 s, one laptop) | 1 replica: read 199 req/s (p95 63 ms), mixed 64 req/s (p95 200 ms); 2 replicas: read 322 req/s (p95 49 ms), mixed 102 req/s (p95 170 ms); 0 errors |

---

## 7. Known limitations

> **Update (Phase 6B–6D, [phase-6b-6d.md](phase-6b-6d.md)):** resolved since this was written:
> the shared dead-letter exchange (per-consumer routing, ADR 0007), the schema-owning
> application role (runtime role, ADR 0008), stack tests on the dev project (isolated
> `ledgerflow-stacktest`, ADR 0009), and "No CI" (`.github/workflows/`). Lock-file hashes,
> authentication and single-node infrastructure remain open.

- **No authentication or authorization.** One shared demo principal and one shared quota.
  The lab signature is not authentication.
- **The application connects as the schema owner.** Runtime DML-only and migration DDL
  roles are not separated.
- **Shared dead-letter exchange.** Every consumer's DLQ is bound to one *fanout* DLX
  (`ledgerflow.events.dlx`; confirmed with `rabbitmqctl list_bindings`), so a message
  dead-lettered by one consumer is also copied into the other consumers' DLQs. Nothing is
  lost, but DLQ depth over-counts and attribution is ambiguous. A fix needs per-consumer
  DLX arguments, which cannot be changed on an existing quorum queue, so it requires a
  queue migration. It was deliberately not changed in 6A.
- **Single-node RabbitMQ, single PostgreSQL primary, single Redis.** Restarts are survived;
  node or disk loss is not. A cluster was out of scope by design.
- **Lockfiles pin versions but not hashes.** The lab and model integrity checks are not
  signatures.
- **Stack tests still redeploy the dev project** (with their own database) and restart its
  containers.
- **The controller token is readable by any process of the same OS user.**
- **No CI.** No GitHub Actions workflow exists, so nothing runs these checks automatically.
- **Retention:** idempotency records, published outbox rows, receipts and lab runs are
  kept indefinitely.

---

## 8. Required work before public deployment

Do not expose this system beyond loopback until all of these are done:

1. **Authentication and per-principal authorization** (e.g. OAuth2/OIDC or API keys), with
   the principal deriving the idempotency and rate-limit scope (`get_current_principal` is
   already the single seam). Per-principal quotas.
2. **TLS** at the gateway, HSTS and security headers; strip or overwrite inbound
   `X-Forwarded-*` and `X-Lab-Traffic` at the edge.
3. **Separate database roles:** a migration role with DDL rights, a runtime role with DML
   only on the needed tables, and a read-only role for observability.
4. **Real secret management:** no `.env` in production; secrets from a vault or the
   orchestrator; rotation for DB, broker and lab keys; no public default passwords.
5. **Production configuration profile:** `APP_ENV=production` (which already refuses the dev
   surface, the replica header, `DB_ECHO`, the memory limiter and fault injection), plus
   `docs_url=None` or authenticated docs.
6. **Dependency supply chain:** hash-locked requirements (`pip-compile --generate-hashes`),
   pinned image digests, `pip-audit` and `npm audit` in CI, image scanning.
7. **CI pipeline** running ruff, the unit, integration, RabbitMQ and stack suites, the
   frontend lint, typecheck, test and build, and Playwright.
8. **Per-consumer dead-letter exchanges** (queue migration) and DLQ alerting.
9. **Backups and restore drills** for PostgreSQL; retention jobs.
10. **Signed model artifacts**, or a model registry with provenance.
11. **Service discovery instead of fixed container IPs** and readiness-based load-balancer
    membership (see [Phase 3](phase-3-design.md#fixed-ips-are-a-local-workaround-not-a-production-pattern)).
