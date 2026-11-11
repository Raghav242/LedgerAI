# LedgerFlow AI

A transaction processing backend for merchants that hold and move virtual money. It is
built around a PostgreSQL double-entry ledger that never loses or invents money, even
under heavy concurrency and client retries: every transfer either fully happens exactly
once or doesn't happen at all.

LedgerFlow AI exists to explore how real payment systems keep balances correct. That
means row-level locking, durable idempotency, an immutable double-entry ledger, strict
validation of money amounts and database-enforced invariants, all in a clean, layered Python service that is easy to read and extend.
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
- **Consistent errors:** every error shares one JSON shape with a stable machine-readable code.
- **Interactive API docs:** OpenAPI/Swagger UI at `/docs`.

## Architecture

```
FastAPI routes ──► Services ──► Repositories ──► PostgreSQL
 (HTTP, schemas)   (business rules,   (queries only,
                    transactions)      never commit)
```

- **API layer:** thin FastAPI routers. Pydantic schemas validate input. A single error handler maps domain errors to HTTP status codes.
- **Service layer:** holds all business logic and owns the transaction boundary. `TransferService` handles idempotency; a shared `LedgerPoster` performs every money movement (lock in ID order → validate → update balances → write transfer + two ledger entries). Validation rules are pure functions, unit-tested without a database.
- **Persistence:** repositories for accounts, transfers, ledger entries and idempotency records. They flush but never commit; one explicit transaction manager (`atomic()`) commits once per operation, applies a lock timeout and classifies concurrency failures. Typed SQLAlchemy 2.x models; schema managed by Alembic.
- **Money supply:** demo money is moved from a built-in system funding account, so all account balances together always sum to zero.
- **Configuration:** environment-based settings with no hardcoded credentials.

| Layer | Technology |
|---|---|
| Language | Python 3.12+ |
| API | FastAPI, Pydantic v2, Uvicorn |
| Database | PostgreSQL 16, SQLAlchemy 2.x, psycopg 3, Alembic |
| Config | pydantic-settings |
| Tooling | Docker Compose, pytest, httpx, Ruff |

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

# Start PostgreSQL and apply migrations
docker compose up -d --wait postgres
alembic upgrade head

# Run the API
uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000/docs** to explore the API.

Stop the database with `docker compose down`. Add `-v` to also delete its data.

## API

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Service and database health |
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
$base = "http://127.0.0.1:8000/api/v1"
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

**Retrying transfers:** on `409 IDEMPOTENCY_REQUEST_IN_PROGRESS`, `503 CONCURRENCY_CONFLICT`,
`500 OUTCOME_UNKNOWN` or a network timeout, retry with the **same** `Idempotency-Key`; a new
key would be a new transfer. Business rejections (for example insufficient funds) are the
key's final outcome and are replayed as-is; use a new key for a new attempt.

Errors use a consistent format:

```json
{ "error": { "code": "INSUFFICIENT_FUNDS", "message": "Source account has insufficient funds", "details": { "balance_minor": 7450, "requested_minor": 999999 } } }
```

## Testing and demos

With PostgreSQL running:

```powershell
pytest                    # full suite
pytest tests/unit         # fast unit tests, no database needed
pytest -m integration     # PostgreSQL-backed tests only
ruff check .              # lint
ruff format --check .     # formatting
```

Integration tests run against a **separate test database** (`ledgerflow_test`), which the
suite creates and migrates automatically. They never touch the development database: the
suite refuses to run unless the test database name ends in `_test`. The suite also checks
that the ORM models match the migrations. Concurrency tests use real PostgreSQL locks with
up to 100 threads on separate connections, and cover distinct-key and same-key
races, opposite-direction transfers, deadlocks, lock timeouts, crash recovery, ambiguous
commits and the Phase 1 → ledger data migration.

Two demo scripts run against the live API (start it first, with `ENABLE_DEV_ENDPOINTS=true`).
Each run creates its own uniquely named accounts and touches no other merchant data:

```powershell
python scripts/demo_concurrency.py   # 100 concurrent $10 transfers from $100 → exactly 10 succeed
python scripts/demo_idempotency.py   # 20 concurrent copies of one request → exactly one transfer
```

Both print their results, verify balance conservation and ledger reconciliation, and exit
with status 1 if any invariant is violated.

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
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | `10` / `20` | Database connection pool per API process |

## Notes and limitations

- **The funding endpoint creates money from nothing** and is meant for local development only. It is disabled by default, and the app refuses to start if it is enabled while `APP_ENV=production`.
- **No authentication yet.** All callers share one fixed demo identity, which is also the scope of every Idempotency-Key. The scope is set by the server, never by the caller.
- **Idempotency keys are kept indefinitely**; there is no retention job yet.
- **Hot accounts serialize.** Transfers from the same account wait for each other's row lock, which bounds per-account throughput.
- **Phase 1 data:** migration `0002` backfills existing transfers exactly. Balances from unrecorded Phase 1 funding become one labelled `legacy_opening_balance` entry per account, and the migration aborts if a balance cannot be explained.
- USD only.
- On Windows, use `POSTGRES_HOST=127.0.0.1` rather than `localhost`. `localhost` may try IPv6 first and hang against Docker Desktop's port binding.

Design notes: [transactions and data model](docs/phase-1-design.md) and
[concurrency, idempotency and the ledger](docs/phase-2-design.md), which includes diagrams
and the trade-offs behind each decision.
