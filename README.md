# LedgerFlow AI

A transaction processing backend for merchants that hold and move virtual money. It is
built around a PostgreSQL double-entry ledger that never loses or invents money, even
under heavy concurrency and client retries: every transfer either fully happens exactly
once or doesn't happen at all.

LedgerFlow AI exists to explore how real payment systems keep balances correct. That
means row-level locking, durable idempotency, an immutable double-entry ledger, strict
validation of money amounts and database-enforced invariants, all in a clean, layered Python service that is easy to read and extend.
It runs as several stateless replicas behind NGINX, with a Redis-backed rate limiter whose
quota is shared by every replica.
It is aimed at engineers who want a working reference for building a correct money-movement
service.

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
- **Health checks:** `/health/live` (process only) and `/health/ready` (PostgreSQL + Redis).
- **Consistent errors:** every error shares one JSON shape with a stable machine-readable code.
- **Interactive API docs:** OpenAPI/Swagger UI at `/docs`.

## Architecture

```
                 ┌─► api-1 ─┐        ┌─► Redis       (rate-limit buckets only)
client ─► NGINX ─┼─► api-2 ─┼────────┤
   :8080         └─► api-N ─┘        └─► PostgreSQL  (balances, ledger, idempotency)

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
- **Configuration:** environment-based settings with no hardcoded credentials.

| Layer | Technology |
|---|---|
| Language | Python 3.12+ |
| API | FastAPI, Pydantic v2, Uvicorn |
| Database | PostgreSQL 16, SQLAlchemy 2.x, psycopg 3, Alembic |
| Scaling | NGINX 1.28, Redis 7.4 (redis-py), Docker Compose |
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

### Option A: the full stack (NGINX + 2 replicas + Redis + PostgreSQL)

```powershell
docker compose up -d --build --wait     # builds the image, migrates once, starts everything
docker compose ps                       # api-1, api-2, nginx, postgres, redis: healthy
```

The API is at **http://127.0.0.1:8080** (docs at `/docs`). Every response carries an
`X-Served-By` header naming the replica that handled it.

```powershell
docker compose --profile scale4 up -d --wait   # 4 replicas (api-3, api-4 join automatically)
docker compose stop api-2                      # run with a single replica
docker compose logs -f nginx api-1 api-2       # watch routing and requests
docker compose down                            # stop everything; add -v to delete the data
```

### Option B: one local process for debugging

```powershell
docker compose up -d --wait postgres redis
alembic upgrade head
uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8000
```

The API is at **http://127.0.0.1:8000** and uses the stack's Redis through its loopback port.

## API

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Database connectivity |
| `GET` | `/health/live` | Liveness (process only) and replica id |
| `GET` | `/health/ready` | Readiness: PostgreSQL and Redis |
| `POST` | `/api/v1/accounts` | Create an account |
| `GET` | `/api/v1/accounts` | List accounts (`limit`, `offset`) |
| `GET` | `/api/v1/accounts/{id}` | Get an account |
| `GET` | `/api/v1/accounts/{id}/balance` | Get an account balance |
| `POST` | `/api/v1/accounts/{id}/fund` | Add simulated funds (**development only**) |
| `POST` | `/api/v1/transfers` | Transfer funds between accounts (requires `Idempotency-Key`) |
| `GET` | `/api/v1/transfers` | Transfer history (`account_id`, `kind`, `limit`, `offset`) |
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

With PostgreSQL and Redis running (`docker compose up -d --wait postgres redis`):

```powershell
pytest                    # unit + integration + Redis tests (stack tests excluded)
pytest tests/unit         # fast unit tests, no services needed
pytest -m integration     # PostgreSQL-backed tests
pytest -m redis           # real-Redis rate limiter tests (atomicity, shared quota, restarts)
pytest -m stack           # end-to-end through NGINX across replicas (redeploys the stack)
ruff check .; ruff format --check .
```

- **Integration tests** run against a separate database (`ledgerflow_test`), created and
  migrated automatically. The suite refuses to run unless the name ends in `_test`.
  Concurrency tests use real PostgreSQL locks with up to 100 threads on separate connections.
- **Redis tests** use logical database 15 and a unique key prefix per test. Token-bucket
  timing is tested with an injected clock (no sleeps). One test starts and restarts its own
  throwaway Redis container.
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

# Phase 2 demos (default: a local uvicorn on :8000; add --base-url http://127.0.0.1:8080)
python scripts/demo_concurrency.py      # 100 concurrent $10 transfers from $100 → exactly 10
python scripts/demo_idempotency.py      # 20 concurrent copies of one request → one transfer
```

Each demo verifies its invariants (including ledger reconciliation) and exits with status 1
if one is violated. The Phase 3 demos stop or reconfigure containers and refuse to run
against anything except `127.0.0.1`/`localhost`.

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

## Notes and limitations

- **The funding endpoint creates money from nothing** and is meant for local development only. It is disabled by default, and the app refuses to start if it is enabled while `APP_ENV=production`.
- **No authentication yet.** All callers share one fixed demo identity, which is also the scope of every Idempotency-Key. The scope is set by the server, never by the caller.
- **Idempotency keys are kept indefinitely**; there is no retention job yet.
- **Hot accounts serialize.** Transfers from the same account wait for each other's row lock, which bounds per-account throughput no matter how many replicas run.
- **Scaling is bounded by shared dependencies.** All replicas share one PostgreSQL primary and one Redis; on a single machine they also share its CPUs. See the measured benchmarks.
- **Redis restarts reset rate limits** (buckets start full): a bounded over-admission of at most one burst. Redis holds no financial data.
- **Replica count is explicit** (`api-1` … `api-4`); adding a fifth means adding a service and one NGINX `server` line.
- **Fixed container IPs are a local workaround.** They avoid two Docker Desktop DNS problems measured during development (slow lookups for stopped containers, stale upstream addresses after restarts). Production systems use dynamic addresses with service discovery and readiness-based load-balancer membership instead; see [the design notes](docs/phase-3-design.md#fixed-ips-are-a-local-workaround-not-a-production-pattern).
- **Phase 1 data:** migration `0002` backfills existing transfers exactly. Balances from unrecorded Phase 1 funding become one labelled `legacy_opening_balance` entry per account, and the migration aborts if a balance cannot be explained.
- USD only.
- On Windows, use `POSTGRES_HOST=127.0.0.1` rather than `localhost`. `localhost` may try IPv6 first and hang against Docker Desktop's port binding.

Design notes: [transactions and data model](docs/phase-1-design.md),
[concurrency, idempotency and the ledger](docs/phase-2-design.md) and
[horizontal scaling and distributed rate limiting](docs/phase-3-design.md), with diagrams
and the trade-offs behind each decision.

To change runtime dependencies, edit `pyproject.toml` and regenerate the image's pinned
`requirements.lock` inside the same base image:

```powershell
docker run --rm -v "${PWD}:/src" python:3.13-slim sh -c "pip install -q /src && pip freeze --exclude ledgerflow-ai"
```
