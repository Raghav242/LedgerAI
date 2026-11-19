# LedgerFlow AI

A transaction processing backend for merchants that hold and move virtual money. It is
built around a PostgreSQL double-entry ledger that never loses or invents money, even
under heavy concurrency and client retries: every transfer either fully happens exactly
once or doesn't happen at all.

LedgerFlow AI exists to explore how real payment systems keep balances correct. That
means row-level locking, durable idempotency, an immutable double-entry ledger, strict
validation of money amounts and database-enforced invariants, all in a clean, layered Python service that is easy to read and extend.
It runs as several stateless replicas behind NGINX, with a Redis-backed rate limiter whose
quota is shared by every replica, and publishes every committed money movement as an event
through a transactional outbox and RabbitMQ.
It is aimed at engineers who want a working reference for building a correct money-movement
service.

A React frontend, the **Distributed Systems Lab**, makes all of this observable: create
accounts, add simulated funds, send idempotent transfers and watch which replica answered,
what the rate limiter said, and how the double-entry ledger and reconciliation stay
consistent. All money is simulated virtual USD.

![LedgerFlow AI overview](docs/screenshots/overview.png)

## Features

- **Virtual accounts:** create, look up and list merchant accounts with USD balances that start at zero.
- **Atomic transfers:** balance changes, the transfer record, its ledger entries and its idempotency outcome commit together in one PostgreSQL transaction, or roll back together on any failure.
- **Concurrency-safe:** accounts are row-locked (`SELECT … FOR UPDATE`) in a deterministic order before balances are checked, so concurrent transfers cannot overdraw an account, lose updates or deadlock each other.
- **Idempotent transfers:** every transfer needs an `Idempotency-Key`. Retries and duplicate concurrent requests return the original response and move money only once; reusing a key with a different body is rejected.
- **Double-entry ledger:** every movement writes one debit and one credit that sum to zero. Ledger entries and transfers are append-only, enforced by database triggers.
- **Reconciliation:** an endpoint compares every stored balance with its ledger-derived balance over one consistent snapshot.
- **Strict validation:** rejects non-positive or fractional amounts, transfers to the same account, unknown accounts, currency mismatches and insufficient funds.
- **Exact money handling:** all amounts are integer minor units (cents), never floating point.
- **Database safeguards:** CHECK constraints prevent negative balances and invalid transfers even if application logic fails.
- **Transfer history:** immutable records, newest first, filterable by account and kind (`transfer`, `dev_funding`, `legacy_opening_balance`).
- **Horizontally scalable:** stateless FastAPI replicas behind an NGINX round-robin load balancer. Any replica can serve any request, including idempotent retries, and a replica can die mid-traffic without corrupting state.
- **Distributed rate limiting:** an atomic Redis token bucket (Lua script, Redis server time, exact integer arithmetic) enforces one quota across all replicas. Writes fail closed (`503`) if Redis is unavailable, reads fail open, and a rate-limited request never opens a database session.
- **Reliable events:** every committed transfer and funding writes a versioned event (`transfer.completed`, `account.funded`) into a transactional outbox in the same PostgreSQL transaction. A publisher delivers it to RabbitMQ with publisher confirms, and an idempotent consumer applies it exactly once despite at-least-once delivery, with bounded retries and a dead-letter queue. The API never depends on the broker.
- **Distributed Systems Lab frontend:** React + TypeScript dashboard with a transaction playground (idempotency-key lifecycle, retry-with-same-key, replay, 429/503 handling), transaction history with ledger drill-down, and live reconciliation. See [Frontend](#frontend-distributed-systems-lab).
- **Health checks:** `/health/live` (process only) and `/health/ready` (PostgreSQL + Redis).
- **Consistent errors:** every error shares one JSON shape with a stable machine-readable code.
- **Interactive API docs:** OpenAPI/Swagger UI at `/docs`.

## Architecture

```
                 ┌─► api-1 ─┐        ┌─► Redis       (rate-limit buckets only)
client ─► NGINX ─┼─► api-2 ─┼────────┤
   :8080         └─► api-N ─┘        └─► PostgreSQL  (balances, ledger, idempotency, outbox)
                                                │
                    outbox-publisher ◄──────────┘ poll (SKIP LOCKED, lease)
                          │ confirms
                          ▼
                     RabbitMQ ──► event-consumer ──► PostgreSQL (receipt + effect, one txn)
                          └─► retry tiers / DLQ

inside each replica:
FastAPI routes ──► rate limit ──► Services ──► Repositories ──► PostgreSQL
 (HTTP, schemas)   (Redis, before   (business rules,   (queries only,
                    any DB work)     transactions)      never commit)
```

- **API layer:** thin FastAPI routers. Pydantic schemas validate input. A single error handler maps domain errors to HTTP status codes.
- **Service layer:** holds all business logic and owns the transaction boundary. `TransferService` handles idempotency; a shared `LedgerPoster` performs every money movement (lock in ID order → validate → update balances → write transfer + two ledger entries). Validation rules are pure functions, unit-tested without a database.
- **Persistence:** repositories for accounts, transfers, ledger entries and idempotency records. They flush but never commit; one explicit transaction manager (`atomic()`) commits once per operation, applies a lock timeout and classifies concurrency failures. Typed SQLAlchemy 2.x models; schema managed by Alembic.
- **Money supply:** demo money is moved from a built-in system funding account, so all account balances together always sum to zero.
- **Scaling:** one Docker image runs every replica plus a one-shot `migrate` job (replicas never migrate). Only NGINX publishes an API port; replicas are reachable only on the internal network.
- **Rate limiting:** a small `RateLimiter` protocol with a Redis token bucket (production) and an in-memory fixed window (comparison and tests), applied as route dependencies with separate `read` and `write` policies.
- **Events:** `LedgerPoster` writes the outbox row next to the ledger entries. `outbox-publisher` and `event-consumer` are separate processes from the same image (`python -m app.events.publisher` / `python -m app.events.consumer`), using synchronous `pika` to match the synchronous codebase.
- **Configuration:** environment-based settings with no hardcoded credentials.

| Layer | Technology |
|---|---|
| Language | Python 3.12+ |
| API | FastAPI, Pydantic v2, Uvicorn |
| Database | PostgreSQL 16, SQLAlchemy 2.x, psycopg 3, Alembic |
| Scaling | NGINX 1.28, Redis 7.4 (redis-py), Docker Compose |
| Events | RabbitMQ 4.1 (quorum queues), pika |
| Config | pydantic-settings |
| Tooling | pytest, httpx, Ruff, Locust |

## Getting started

**Prerequisites:** Python 3.12+ and Docker Desktop.

```powershell
git clone https://github.com/Raghav242/LedgerAI.git
cd LedgerAI

# Create a virtual environment and install dependencies
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1          # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"

# Configure (local, non-secret defaults)
Copy-Item .env.example .env           # macOS/Linux: cp .env.example .env
```

### Option A: the full stack (NGINX + 2 replicas + Redis + PostgreSQL + RabbitMQ + workers)

```powershell
docker compose up -d --build --wait     # builds the image, migrates once, starts everything
docker compose ps                       # api-1, api-2, nginx, postgres, redis, rabbitmq,
                                        # outbox-publisher, event-consumer: healthy
```

The API is at **http://127.0.0.1:8080** (docs at `/docs`). Every response carries an
`X-Served-By` header naming the replica that handled it. The RabbitMQ management UI is at
**http://127.0.0.1:15672** (credentials from `.env`).

```powershell
$env:NGINX_UPSTREAMS="4"; docker compose --profile scale4 up -d --wait   # 4 replicas
Remove-Item Env:NGINX_UPSTREAMS; docker compose --profile scale4 rm -sf api-3 api-4; docker compose up -d --wait   # back to 2
docker compose stop api-2                      # run with a single replica
docker compose logs -f nginx api-1 api-2       # watch routing and requests
docker compose logs -f outbox-publisher event-consumer   # watch events flow
docker compose restart event-consumer          # each process restarts independently
docker compose down                            # stop everything; add -v to delete the data
```

### Option B: one local process for debugging

```powershell
docker compose up -d --wait postgres redis
alembic upgrade head
uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8000
```

The API is at **http://127.0.0.1:8000** and uses the stack's Redis through its loopback port.

## Frontend (Distributed Systems Lab)

**Prerequisites:** Node.js 20.19+ (22 LTS tested) and the backend stack running (Option A).

```powershell
docker compose up -d --build --wait     # backend: NGINX on 127.0.0.1:8080
cd frontend
npm install
npm run dev                             # http://127.0.0.1:5173
```

The dev server proxies `/api` and `/health` to NGINX at `127.0.0.1:8080` (override with
`LEDGERFLOW_API_PROXY_TARGET` in `frontend/.env.local`; see `frontend/.env.example`), so the
browser makes same-origin requests and the backend needs no CORS configuration. Adding
simulated funds requires `ENABLE_DEV_ENDPOINTS=true` (the default in `.env.example`).

| Page | Status | What it shows |
|---|---|---|
| Overview | live | account/transfer/volume totals, reconciliation and API status, 14-day activity, recent transactions |
| Transaction Playground | live | create and fund accounts; transfers with a visible Idempotency-Key, outcome classification, same-key retry and replay, serving replica, rate-limit headers, attempt timeline |
| Transaction History | live | filter by account/type, server-side pagination, detail drawer with the transfer's ledger entries |
| Ledger & Reconciliation | live | double-entry explainer (labelled example), live reconciliation invariants, paged ledger postings |
| Experiment Lab, System Topology, AI Anomaly Lab, Performance Lab | planned | roadmap pages, clearly marked; no fake controls |

```powershell
npm run typecheck      # tsc, strict
npm run lint           # ESLint
npm test               # Vitest + React Testing Library (mocked HTTP)
npm run build          # production bundle in frontend/dist
npm run preview        # serve the build on http://127.0.0.1:4173 (same proxy)
npm run test:e2e       # Playwright against the REAL stack (uses installed Edge; PW_CHANNEL=chrome for Chrome)
npm run gen:api        # regenerate src/types/openapi.gen.ts from the running backend's OpenAPI
```

The end-to-end test creates uniquely named `E2E …` accounts with simulated money in the
development database. Design notes: [docs/phase-5a-design.md](docs/phase-5a-design.md).

> **Do not expose the frontend or the API publicly.** There is no authentication: every
> caller acts as the demo merchant, and the funding endpoint mints simulated money. Both
> servers bind to `127.0.0.1` by default.

## API

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Database connectivity |
| `GET` | `/health/live` | Liveness (process only) and replica id |
| `GET` | `/health/ready` | Readiness: PostgreSQL and Redis |
| `POST` | `/api/v1/accounts` | Create an account |
| `GET` | `/api/v1/accounts` | List accounts (`limit`, `offset`, `order=asc\|desc`) |
| `GET` | `/api/v1/accounts/{id}` | Get an account |
| `GET` | `/api/v1/accounts/{id}/balance` | Get an account balance |
| `POST` | `/api/v1/accounts/{id}/fund` | Add simulated funds (**development only**) |
| `POST` | `/api/v1/transfers` | Transfer funds between accounts (requires `Idempotency-Key`) |
| `GET` | `/api/v1/transfers` | Transfer history (`account_id`, `kind`, `limit`, `offset`) |
| `GET` | `/api/v1/transfers/{id}` | Get one transfer |
| `GET` | `/api/v1/transfers/{id}/ledger-entries` | The transfer's debit and credit postings |
| `GET` | `/api/v1/ledger/entries` | Ledger postings, newest first (`account_id`, `limit`, `offset`) |
| `GET` | `/api/v1/stats/summary` | Totals by transfer kind and daily activity (`activity_days`, one snapshot) |
| `GET` | `/api/v1/reconciliation` | Check balances against the ledger |

Amounts are integers in minor units, so `2550` means $25.50.

```powershell
$base = "http://127.0.0.1:8080/api/v1"   # NGINX (use :8000 for a local uvicorn)
$a = Invoke-RestMethod -Method Post "$base/accounts" -ContentType "application/json" -Body '{"name":"Alice Coffee"}'
$b = Invoke-RestMethod -Method Post "$base/accounts" -ContentType "application/json" -Body '{"name":"Bob Bakery"}'
Invoke-RestMethod -Method Post "$base/accounts/$($a.id)/fund" -ContentType "application/json" -Body '{"amount_minor":10000}'

$transfer = @{ source_account_id = $a.id; destination_account_id = $b.id; amount_minor = 2550; currency = "USD" } | ConvertTo-Json
$headers = @{ "Idempotency-Key" = [guid]::NewGuid().ToString() }
Invoke-RestMethod -Method Post "$base/transfers" -ContentType "application/json" -Body $transfer -Headers $headers
# Sending the same request again with the same $headers replays the original response
# (header Idempotent-Replayed: true) and moves no money.

Invoke-RestMethod "$base/reconciliation"   # ok: True
```

**Retrying transfers:** on `429 RATE_LIMITED` (wait `Retry-After`), `409 IDEMPOTENCY_REQUEST_IN_PROGRESS`,
`503 CONCURRENCY_CONFLICT`, `503 RATE_LIMITER_UNAVAILABLE`, `500 OUTCOME_UNKNOWN`, a `502`/`504`
from the load balancer or a network timeout, retry with the **same** `Idempotency-Key`; a new
key would be a new transfer. Business rejections (for example insufficient funds) are the
key's final outcome and are replayed as-is; use a new key for a new attempt.

**Rate limits:** POST routes share a `write` token bucket (default: bursts of 20, then 10/s)
and GET routes a `read` bucket (100, then 50/s), per merchant, across all replicas.
Responses carry `X-RateLimit-Policy`, `X-RateLimit-Limit` and `X-RateLimit-Remaining`;
429s add `Retry-After`. Every admitted request costs a token, including idempotent replays.

Errors use a consistent format:

```json
{ "error": { "code": "INSUFFICIENT_FUNDS", "message": "Source account has insufficient funds", "details": { "balance_minor": 7450, "requested_minor": 999999 } } }
```

## Testing and demos

With PostgreSQL, Redis and RabbitMQ running (`docker compose up -d --wait postgres redis rabbitmq`):

```powershell
pytest                    # unit + integration + Redis tests (stack tests excluded)
pytest tests/unit         # fast unit tests, no services needed
pytest -m integration     # PostgreSQL-backed tests
pytest -m redis           # real-Redis rate limiter tests (atomicity, shared quota, restarts)
pytest -m rabbitmq        # real-RabbitMQ delivery tests (dedup, crashes, retries, DLQ, outage)
pytest -m stack           # end-to-end through NGINX across replicas (redeploys the stack)
ruff check .; ruff format --check .
```

- **Integration tests** run against a separate database (`ledgerflow_test`), created and
  migrated automatically. The suite refuses to run unless the name ends in `_test`.
  Concurrency tests use real PostgreSQL locks with up to 100 threads on separate connections.
- **Redis tests** use logical database 15 and a unique key prefix per test. Token-bucket
  timing is tested with an injected clock (no sleeps). One test starts and restarts its own
  throwaway Redis container.
- **RabbitMQ tests** give every test its own exchange/queue name prefix on the stack's
  broker and delete it afterwards. The broker-outage test starts and restarts its own
  throwaway RabbitMQ container. Crashes are simulated in-process (no ack, rollback) and,
  in the demos, with real process deaths.
- **Stack tests** temporarily redeploy the Compose stack against a separate database
  (`ledgerflow_stacktest`) with a deterministic quota, then restore the default stack. They
  verify that both replicas serve traffic, that the quota is global, that idempotent
  retries work across replicas, and that state survives killing a replica mid-traffic.

Demo scripts run against the live API and create their own uniquely named accounts:

```powershell
# Through NGINX (full stack)
python scripts/demo_load_balancing.py   # round robin, stop a replica, recover
python scripts/demo_rate_limiting.py    # 50 concurrent writes vs a quota of 10, global; refill
python scripts/demo_redis_failure.py    # stop Redis: writes 503 fast, reads up, clean recovery
python scripts/demo_outbox_recovery.py  # stop RabbitMQ: transfers continue, events delivered later, once
python scripts/demo_event_deduplication.py  # one event delivered 7 times, applied once
python scripts/demo_worker_crash.py     # consumer really dies before/after commit; effect exactly once

# Phase 2 demos (default: a local uvicorn on :8000; add --base-url http://127.0.0.1:8080)
python scripts/demo_concurrency.py      # 100 concurrent $10 transfers from $100 → exactly 10
python scripts/demo_idempotency.py      # 20 concurrent copies of one request → one transfer
```

Each demo verifies its invariants (including ledger reconciliation) and exits with status 1
if one is violated. Demos that stop or reconfigure containers refuse to run against
anything except `127.0.0.1`/`localhost`, and restore what they stopped.

**Benchmarks** (Locust; redeploy 1/2/4 replicas against a separate `ledgerflow_bench`
database, then restore the default stack):

```powershell
python benchmarks/run_benchmarks.py --duration 30 --users 64 --replicas 1 2 4
```

Measured results and their analysis are in [docs/phase-3-design.md](docs/phase-3-design.md#13-benchmarks).

## Configuration

Settings are read from environment variables or a `.env` file. See [`.env.example`](.env.example).

| Variable | Default | Purpose |
|---|---|---|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` | *required* | Database credentials (shared with Docker Compose) |
| `POSTGRES_HOST` / `POSTGRES_PORT` | `127.0.0.1` / `5432` | Database location |
| `POSTGRES_DB` | `ledgerflow` | Development database |
| `POSTGRES_TEST_DB` | `ledgerflow_test` | Integration test database |
| `ENABLE_DEV_ENDPOINTS` | `false` | Enables the funding endpoint |
| `APP_ENV` | `development` | `development`, `test` or `production` |
| `LOG_LEVEL` | `INFO` | Application log level |
| `TRANSFER_LOCK_TIMEOUT_MS` | `3000` | Max wait for a row lock or an in-flight same-key request |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | `10` / `20` | Pool for a locally run process (Compose replicas: `API_DB_POOL_SIZE` / `API_DB_MAX_OVERFLOW`, default `5` / `5`) |
| `DB_POOL_TIMEOUT_SECONDS` / `DB_POOL_RECYCLE_SECONDS` | `30` / `1800` | Wait for a pooled connection; recycle old connections |
| `RATE_LIMIT_BACKEND` | `redis` | `redis` (shared) or `memory` (per process; refused in production) |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | Redis for rate limiting (replicas use `redis:6379` internally) |
| `REDIS_CONNECT_TIMEOUT_MS` / `REDIS_SOCKET_TIMEOUT_MS` | `250` / `250` | Upper bound on any rate-limit check |
| `RATE_LIMIT_WRITE_*` / `RATE_LIMIT_READ_*` | `20,10,1000` / `100,50,1000` | `CAPACITY`, `REFILL_TOKENS`, `REFILL_INTERVAL_MS` of each token bucket |
| `EXPOSE_REPLICA_HEADER` | `false` (stack: `true`) | Add `X-Served-By` to responses |
| `NGINX_PORT` / `NGINX_BIND` | `8080` / `127.0.0.1` | Published load-balancer address |
| `BACKEND_NET` | `172.28.0` | Internal `/24` network for fixed container addresses (change only on collision) |
| `NGINX_UPSTREAMS` | `2` | Which upstream list NGINX loads (`2` or `4`); must match the deployed replicas |
| `RABBITMQ_USER` / `RABBITMQ_PASSWORD` | `ledgerflow` / *required for the stack* | Broker credentials (publisher, consumer, broker) |
| `RABBITMQ_HOST` / `RABBITMQ_PORT` | `127.0.0.1` / `5672` | Broker for host-run tests/demos (workers use `rabbitmq:5672`) |
| `OUTBOX_BATCH_SIZE` / `OUTBOX_LEASE_SECONDS` | `100` / `30` | Publisher batch size and claim lease |
| `CONSUMER_PREFETCH` | `10` | Max unacknowledged messages per consumer |
| `CONSUMER_RETRY_DELAYS_MS` | `[1000, 5000, 25000]` | One retry tier per entry; after the last, the DLQ |
| `CONSUMER_DELIVERY_LIMIT` | `5` | Broker dead-letters a message redelivered more often than this |

## Notes and limitations

- **The funding endpoint creates money from nothing** and is meant for local development only. It is disabled by default, and the app refuses to start if it is enabled while `APP_ENV=production`.
- **No authentication yet.** All callers share one fixed demo identity, which is also the scope of every Idempotency-Key. The scope is set by the server, never by the caller.
- **Idempotency keys are kept indefinitely**; there is no retention job yet.
- **Hot accounts serialize.** Transfers from the same account wait for each other's row lock, which bounds per-account throughput no matter how many replicas run.
- **Scaling is bounded by shared dependencies.** All replicas share one PostgreSQL primary and one Redis; on a single machine they also share its CPUs. See the measured benchmarks.
- **Redis restarts reset rate limits** (buckets start full): a bounded over-admission of at most one burst. Redis holds no financial data.
- **Replica count is explicit** (`api-1` … `api-4`), and NGINX's upstream file must list exactly the deployed replicas (`NGINX_UPSTREAMS`).
- **Events are at-least-once.** A broker or worker crash can deliver an event twice; consumers deduplicate by event id. There is no ordering guarantee across events.
- **RabbitMQ is a single node, not highly available.** It survives restarts (durable queues, persistent messages), but losing the node or its disk loses published-but-unconsumed messages. Unpublished events are always safe in the outbox.
- **Events start with migration `0003`.** Transfers committed before it have no events. Published outbox rows and receipts are kept indefinitely (no retention job yet).
- **Fixed container IPs are a local workaround.** They avoid two Docker Desktop DNS problems measured during development (slow lookups for stopped containers, stale upstream addresses after restarts). Production systems use dynamic addresses with service discovery and readiness-based load-balancer membership instead; see [the design notes](docs/phase-3-design.md#fixed-ips-are-a-local-workaround-not-a-production-pattern).
- **Phase 1 data:** migration `0002` backfills existing transfers exactly. Balances from unrecorded Phase 1 funding become one labelled `legacy_opening_balance` entry per account, and the migration aborts if a balance cannot be explained.
- **Frontend lists are bounded.** The account picker shows the newest 100 accounts (no search endpoint), and list endpoints return no total counts, so history uses previous/next pagination.
- USD only.
- On Windows, use `POSTGRES_HOST=127.0.0.1` rather than `localhost`. `localhost` may try IPv6 first and hang against Docker Desktop's port binding.

Design notes: [transactions and data model](docs/phase-1-design.md),
[concurrency, idempotency and the ledger](docs/phase-2-design.md) and
[horizontal scaling and distributed rate limiting](docs/phase-3-design.md) and
[transactional outbox and reliable events](docs/phase-4a-design.md) and
[the frontend and transaction playground](docs/phase-5a-design.md), with diagrams
and the trade-offs behind each decision.

To change runtime dependencies, edit `pyproject.toml` and regenerate the image's pinned
`requirements.lock` inside the same base image:

```powershell
docker run --rm -v "${PWD}:/src" python:3.13-slim sh -c "pip install -q /src && pip freeze --exclude ledgerflow-ai"
```
