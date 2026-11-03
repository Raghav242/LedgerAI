# LedgerFlow AI

A transaction processing backend for merchants that hold and move virtual money. It is
built around a PostgreSQL ledger that never loses or invents money: every transfer either
fully happens or doesn't happen at all.

LedgerFlow AI exists to explore how real payment systems keep balances correct. That
means transactional integrity, strict validation of money amounts, and database-enforced
invariants, all in a clean, layered Python service that is easy to read and extend.
It is aimed at engineers who want a working reference for building a correct money-movement
service.

## Features

- **Virtual accounts:** create, look up and list merchant accounts with USD balances that start at zero.
- **Atomic transfers:** the debit, the credit and the transfer record commit together in one PostgreSQL transaction, or roll back together on any failure.
- **Strict validation:** rejects non-positive or fractional amounts, transfers to the same account, unknown accounts, currency mismatches and insufficient funds.
- **Exact money handling:** all amounts are integer minor units (cents), never floating point.
- **Database safeguards:** CHECK constraints prevent negative balances and invalid transfers even if application logic fails.
- **Transfer history:** immutable transfer records, newest first, filterable by account.
- **Consistent errors:** every error shares one JSON shape with a stable machine-readable code.
- **Interactive API docs:** OpenAPI/Swagger UI at `/docs`.

## Architecture

```
FastAPI routes ──► Services ──► Repositories ──► PostgreSQL
 (HTTP, schemas)   (business rules,   (queries only,
                    transactions)      never commit)
```

- **API layer:** thin FastAPI routers. Pydantic schemas validate input. A single error handler maps domain errors to HTTP status codes.
- **Service layer:** holds all business logic and owns the transaction boundary. Validation rules are pure functions, unit-tested without a database.
- **Persistence:** typed SQLAlchemy 2.x models. The schema is managed by Alembic migrations.
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
| `POST` | `/api/v1/transfers` | Transfer funds between accounts |
| `GET` | `/api/v1/transfers` | Transfer history (`account_id`, `limit`, `offset`) |

Amounts are integers in minor units, so `2550` means $25.50.

```powershell
$base = "http://127.0.0.1:8000/api/v1"
$a = Invoke-RestMethod -Method Post "$base/accounts" -ContentType "application/json" -Body '{"name":"Alice Coffee"}'
$b = Invoke-RestMethod -Method Post "$base/accounts" -ContentType "application/json" -Body '{"name":"Bob Bakery"}'
Invoke-RestMethod -Method Post "$base/accounts/$($a.id)/fund" -ContentType "application/json" -Body '{"amount_minor":10000}'

$transfer = @{ source_account_id = $a.id; destination_account_id = $b.id; amount_minor = 2550; currency = "USD" } | ConvertTo-Json
Invoke-RestMethod -Method Post "$base/transfers" -ContentType "application/json" -Body $transfer
```

Errors use a consistent format:

```json
{ "error": { "code": "INSUFFICIENT_FUNDS", "message": "Source account has insufficient funds", "details": { "balance_minor": 7450, "requested_minor": 999999 } } }
```

## Testing

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
that the ORM models match the migrations.

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

## Notes and limitations

- **The funding endpoint creates money from nothing** and is meant for local development only. It is disabled by default, and the app refuses to start if it is enabled while `APP_ENV=production`.
- **Concurrent transfers are not yet safe.** Transfers are atomic, but simultaneous transfers from the same account are not serialized, so balance updates can be lost under concurrent load.
- **Retries are not deduplicated.** A client retrying a timed-out request may create a duplicate transfer.
- USD only. There is no authentication or authorization.
- On Windows, use `POSTGRES_HOST=127.0.0.1` rather than `localhost`. `localhost` may try IPv6 first and hang against Docker Desktop's port binding.

For the reasoning behind the main design decisions, see [docs/phase-1-design.md](docs/phase-1-design.md).
