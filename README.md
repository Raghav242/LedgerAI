# LedgerFlow AI

A virtual-money transaction processing platform for merchants, built incrementally as a
hands-on study of system design, low-level design, concurrency, reliability and AI infrastructure.

> **Current state: Phase 1, backend foundation and basic transaction engine.**
> Transfers are **atomic** (all-or-nothing) but **not yet concurrency-safe**. See [Current limitations](#9-current-limitations).

---

## 1. Project purpose

Merchants hold virtual USD accounts and move money between them. Phase 1 builds the
foundation every later phase depends on:

- a correct, transactional PostgreSQL ledger of accounts and transfers
- a layered FastAPI service with strict input validation and consistent errors
- a test suite that runs against a real, isolated PostgreSQL database

## 2. Phase 1 architecture

A **modular monolith** with one-directional dependencies:

```
HTTP request
   │
   ▼
┌──────────────┐  app/api/          FastAPI routers, DI wiring, error → HTTP mapping
│     API      │  (thin: parse, call service, serialize)
└──────┬───────┘
       │ Pydantic schemas (app/schemas/) define the HTTP contract
       ▼
┌──────────────┐  app/services/     Business rules + transaction boundary (atomic())
│   Services   │  transfer_rules.py is pure (no DB) and unit-tested on its own
└──────┬───────┘
       ▼
┌──────────────┐  app/repositories/ Queries; add + flush, NEVER commit
│ Repositories │  app/models/       SQLAlchemy 2.x typed ORM models
└──────┬───────┘
       ▼
   PostgreSQL 16 (CHECK constraints, FKs; schema owned by Alembic)

app/core/  config (pydantic-settings) · engine/session factory · domain exceptions · logging
```

```
LedgerAI/
├── app/
│   ├── main.py                     # create_app() factory, lifespan, router wiring
│   ├── api/
│   │   ├── dependencies.py         # per-request Session, service providers, paging params
│   │   ├── error_handlers.py       # DomainError/validation/HTTP/500 → {"error": {...}}
│   │   ├── router.py               # /api/v1 router; dev routes only if enabled
│   │   └── routes/                 # health.py · accounts.py · transfers.py
│   ├── core/                       # config.py · database.py · exceptions.py · logging.py
│   ├── models/                     # base.py · enums.py · account.py · transfer.py
│   ├── repositories/               # account_repository.py · transfer_repository.py
│   ├── schemas/                    # common.py · account.py · transfer.py
│   └── services/                   # account_service.py · transfer_service.py · transfer_rules.py
├── alembic/                        # env.py + versions/ (hand-written 0001 migration)
├── tests/
│   ├── unit/                       # rules, schemas, config: no database needed
│   └── integration/                # real PostgreSQL test DB: API, rollback, constraints
├── docs/phase-1-design.md
├── docker-compose.yml · alembic.ini · pyproject.toml · .env.example
```

## 3. Technology choices

| Concern | Choice | Why |
|---|---|---|
| API | FastAPI + Pydantic v2 | Typed request validation and OpenAPI docs come with it |
| ORM | SQLAlchemy 2.x (sync) | Explicit sessions/transactions; typed `Mapped[]` models |
| Driver | psycopg 3 | Current, maintained PostgreSQL driver |
| DB | PostgreSQL 16 | ACID transactions, CHECK constraints, row locks for Phase 2 |
| Migrations | Alembic | Versioned, reviewable schema changes |
| Config | pydantic-settings | Typed env config; secrets as `SecretStr` |
| Tests | pytest + FastAPI TestClient (httpx) | Real HTTP semantics against a real DB |
| Lint/format | Ruff | One fast tool for both |

The route handlers are synchronous on purpose. FastAPI runs them in a threadpool, and that
keeps the transaction code free of async ORM complexity. `pytest-asyncio` is therefore
not needed in this phase.

## 4. Setup (Windows 11 / PowerShell)

Prerequisites: Python 3.12+ (the `py` launcher), Docker Desktop (WSL2 backend) running.

```powershell
# 1. Virtual environment + dependencies
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"

# 2. Local configuration (non-secret dev defaults)
Copy-Item .env.example .env

# 3. Start PostgreSQL and wait until it is healthy
docker compose up -d --wait postgres
docker compose ps

# 4. Apply migrations to the development database
alembic upgrade head

# 5. Run the API (auto-reload). Docs: http://127.0.0.1:8000/docs
uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8000

# 6. Tests (auto-creates and migrates the separate ledgerflow_test database)
pytest

# 7. Lint / format
ruff check .
ruff format --check .

# 8. Stop infrastructure (data is kept in the named volume)
docker compose down
# ...or wipe the database volume entirely:
docker compose down -v
```

> If `Activate.ps1` is blocked, run once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
>
> Use `POSTGRES_HOST=127.0.0.1`, not `localhost`. On Windows, `localhost` tries IPv6 `::1`
> first, and that attempt hangs against Docker Desktop's IPv4 port binding.

## 5. API examples (PowerShell)

All amounts are **integer minor units**: `2550` means **$25.50**.

```powershell
$base = "http://127.0.0.1:8000"

Invoke-RestMethod "$base/health"

$alice = Invoke-RestMethod -Method Post "$base/api/v1/accounts" `
  -ContentType "application/json" -Body '{"name": "Alice Coffee"}'
$bob = Invoke-RestMethod -Method Post "$base/api/v1/accounts" `
  -ContentType "application/json" -Body '{"name": "Bob Bakery"}'

# DEVELOPMENT ONLY: mint $100.00 of simulated money
Invoke-RestMethod -Method Post "$base/api/v1/accounts/$($alice.id)/fund" `
  -ContentType "application/json" -Body '{"amount_minor": 10000}'

# Transfer $25.50
$body = @{ source_account_id = $alice.id; destination_account_id = $bob.id;
           amount_minor = 2550; currency = "USD" } | ConvertTo-Json
Invoke-RestMethod -Method Post "$base/api/v1/transfers" -ContentType "application/json" -Body $body

Invoke-RestMethod "$base/api/v1/accounts/$($alice.id)/balance"   # balance_minor = 7450
Invoke-RestMethod "$base/api/v1/accounts?limit=20&offset=0"
Invoke-RestMethod "$base/api/v1/transfers?account_id=$($bob.id)"
```

| Method | Path | Success | Errors |
|---|---|---|---|
| GET | `/health` | 200 | 503 if DB unreachable |
| POST | `/api/v1/accounts` | 201 | 422 |
| GET | `/api/v1/accounts?limit&offset` | 200 | 422 |
| GET | `/api/v1/accounts/{id}` | 200 | 404, 422 |
| GET | `/api/v1/accounts/{id}/balance` | 200 | 404, 422 |
| POST | `/api/v1/accounts/{id}/fund` **(dev only)** | 200 | 404, 422 |
| POST | `/api/v1/transfers` | 201 | 404 missing account, 409 insufficient funds, 422 invalid |
| GET | `/api/v1/transfers?account_id&limit&offset` | 200 | 422 |

Every error has the same shape:

```json
{"error": {"code": "INSUFFICIENT_FUNDS",
           "message": "Source account has insufficient funds",
           "details": {"account_id": "…", "balance_minor": 7450, "requested_minor": 999999}}}
```

**The funding endpoint is development-only.** It creates money from nothing. The route is
only registered when `ENABLE_DEV_ENDPOINTS=true`, which defaults to **false**, and the app
refuses to start if that flag is set together with `APP_ENV=production`.

## 6. Database design

```
accounts                                     transfers
─────────────────────────────────────        ──────────────────────────────────────────────
id             UUID PK                 ◄──┐  id                     UUID PK
name           VARCHAR(100) NOT NULL      ├─ source_account_id      UUID FK RESTRICT, indexed
currency       VARCHAR(3)  NOT NULL       └─ destination_account_id UUID FK RESTRICT, indexed
balance_minor  BIGINT NOT NULL DEFAULT 0     amount_minor           BIGINT NOT NULL
created_at     TIMESTAMPTZ DEFAULT now()     currency               VARCHAR(3) NOT NULL
                                             status                 VARCHAR(20) NOT NULL
CHECK balance_minor >= 0                     created_at             TIMESTAMPTZ DEFAULT now(), indexed
CHECK currency IN ('USD')
                                             CHECK amount_minor > 0
                                             CHECK source_account_id <> destination_account_id
                                             CHECK currency IN ('USD')
                                             CHECK status IN ('completed')
```

- All constraint names are deterministic (`ck_accounts_balance_non_negative`, …) via a
  SQLAlchemy naming convention.
- `status` and `currency` are `VARCHAR` + `CHECK` rather than native PG enums. Adding a
  value later is a one-line constraint change, not an `ALTER TYPE`.
- `ON DELETE RESTRICT` means an account with history can never be deleted.

## 7. How transactions are handled

`POST /api/v1/transfers` runs in one PostgreSQL transaction:

1. The request dependency opens a `Session` (`with session_factory() as session`).
2. `TransferService.create_transfer` runs the cheap checks: positive amount, different accounts.
3. Inside `with atomic(session):`
   - load the source and destination (404 if either is missing)
   - check currency and sufficient funds (422 / 409)
   - `source.balance_minor -= amount` and `destination.balance_minor += amount`
   - `TransferRepository.add(...)` flushes the INSERT (UPDATEs and INSERT reach the DB, still uncommitted)
   - leaving the block commits **once**
4. Any exception inside the block (domain error, `IntegrityError` from a CHECK, a crash)
   causes `session.rollback()`, so neither balance nor the transfer row is persisted.
5. When the request ends, the session closes; closing also rolls back anything uncommitted.

Repositories never commit. Only `atomic()` in the service layer does. Details are in
[docs/phase-1-design.md](docs/phase-1-design.md).

## 8. Running tests

```powershell
docker compose up -d --wait postgres
pytest                    # all tests (unit + integration)
pytest tests/unit         # no database needed
pytest -m integration     # only the PostgreSQL-backed tests
```

Safety: integration tests only use `POSTGRES_TEST_DB` (default `ledgerflow_test`). The
suite **aborts** unless that name ends in `_test` and differs from `POSTGRES_DB`. At startup
it creates the database if missing and runs Alembic `downgrade base` → `upgrade head`, so
the real migrations are exercised every run. Tables are truncated after each test.
`alembic check` also runs as a test, so a model change without a migration fails CI.

## 9. Current limitations

- **Not concurrency-safe.** Accounts are read without row locks. Two concurrent transfers
  from the same account can both pass the funds check. The second either violates the CHECK
  constraint (500) or overwrites the first one's balance (a lost update, which breaks money
  conservation). Concurrent dev fundings can lose updates the same way.
- **No idempotency.** A client retry after a timeout can execute a transfer twice.
- **Balance is a mutable column**, not derived from double-entry ledger entries, so there is
  no independent audit trail to reconcile against.
- USD only. Offset pagination only (slows on deep pages). No authentication or authorization.
- An unexpected DB error such as a CHECK violation returns a generic 500. Phase 2 maps
  serialization and lock failures explicitly.

## 10. Planned next phases

1. **Phase 2: Concurrency & correctness.** `SELECT … FOR UPDATE` with deterministic lock
   ordering, idempotency keys, double-entry `ledger_entries`, concurrent stress tests.
2. **Redis rate limiting**, distributed across API instances.
3. **RabbitMQ async events** via a transactional outbox.
4. **Anomaly detection** with scikit-learn on transfer streams.
5. **Multi-instance scaling** behind a load balancer; React + TypeScript frontend.
6. **Observability, benchmarks and fault injection.**
