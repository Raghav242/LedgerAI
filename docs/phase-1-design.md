# Phase 1 Design Notes

These notes explain the *why* behind Phase 1. Each section covers the decision, the
alternatives considered, and what it costs.

## Why PostgreSQL

Moving money needs a database that can change several rows **all-or-nothing** and that
**enforces invariants itself**. PostgreSQL provides:

- **ACID transactions.** The debit, credit and transfer record commit together or not at all.
- **CHECK and foreign-key constraints**, enforced on every write regardless of which code
  path (or human with `psql`) issued it.
- **Row-level locks** (`SELECT … FOR UPDATE`) and serializable isolation, which Phase 2
  needs for concurrency control.
- Mature tooling, free, and runs locally in Docker.

Rejected alternatives: SQLite has a single writer, weak typing and different locking
semantics, so tests against it would prove little about production behaviour. A NoSQL
store would force us to rebuild multi-row atomicity in the application.

## Why SQLAlchemy (2.x, synchronous)

- **Explicit unit of work.** The `Session` tracks changes and flushes them in one
  transaction, and we decide exactly when `commit()` happens.
- **Typed models** (`Mapped[int]`) catch mistakes early and document the schema in code.
- **An escape hatch to SQL.** `select()`, `update()` and `text()` are always available, and
  Phase 2's `with_for_update()` is one method call.
- **Sync over async.** Async SQLAlchemy adds event-loop and lazy-loading pitfalls with no
  benefit at this scale. FastAPI runs sync handlers in a threadpool, so the server still
  handles requests concurrently.

## Why money is integer minor units

`0.1 + 0.2 != 0.3` in binary floating point. Money stored as floats accumulates rounding
error and eventually fails to reconcile. Two exact representations were possible:

| Option | Verdict |
|---|---|
| `float` | ❌ inexact |
| `NUMERIC` / `Decimal` | ✅ exact, but needs a scale convention, Decimal context handling and string serialization in JSON |
| **`BIGINT` minor units (cents)** | ✅ exact, fast integer arithmetic, and JSON integers travel safely |

We chose `BIGINT` with explicit names (`balance_minor`, `amount_minor`) so the unit is
impossible to forget. The API uses Pydantic **strict** integers, so `10.5`, `"1000"` and
`true` are rejected rather than coerced. A per-request cap (`MAX_AMOUNT_MINOR`, $1B) keeps
sums far from the BIGINT limit. Display formatting ($25.50) is a client concern.

## Why a transfer needs one atomic transaction

A transfer is three writes: debit source, credit destination, insert transfer record. If
they committed separately, a crash between them would leave:

- money debited but never credited (money destroyed), or
- a credit with no debit (money created), or
- balances changed with no record explaining why.

The invariant *the sum of all balances is constant* only holds if all three writes become
visible at the same instant, and that is exactly what a database transaction guarantees.

## How rollback works

```python
# app/core/database.py
@contextmanager
def atomic(session):
    try:
        yield
        session.commit()  # one commit for the whole block
    except Exception:
        session.rollback()  # undo every flushed and unflushed change
        raise
```

- Inside the block, SQLAlchemy may *flush* (send UPDATE/INSERT to PostgreSQL) several
  times. Flushed rows are visible only inside this transaction until `COMMIT`.
- If anything raises (a business rule, a CHECK violation, a bug), `rollback()` tells
  PostgreSQL to discard the transaction, and SQLAlchemy expires the in-memory objects so
  stale values aren't reused.
- The request-scoped session is opened in a `with` block (`get_session`). Closing it
  releases the connection back to the pool and rolls back anything left uncommitted. This is
  a second safety net in case a code path forgets to commit or roll back.

**Rule:** repositories call `flush()` at most, never `commit()`. Only services, through
`atomic()`, end a transaction. That makes it impossible for a helper to commit half a transfer.

Two integration tests prove this:

- `test_failure_after_debit_rolls_back_everything` injects a crash *after* both balances
  were changed in memory. Both balances stay unchanged and zero transfer rows exist.
- `test_database_check_constraint_rolls_back_whole_transfer` disables the application's
  funds check. PostgreSQL's `ck_accounts_balance_non_negative` rejects the overdraft, and
  the credit plus the transfer row roll back with it (defense in depth).

### Why `atomic()` instead of `with session.begin():`

`session.begin()` does the same thing, but it raises if the session has already
auto-begun a transaction (for example after an earlier read). `atomic()` works in both
situations and keeps the commit/rollback rule visible in one place.

## Why business logic is separated from API routes

- **Testability.** `transfer_rules.py` is pure functions over `Account` objects. It is
  unit-tested with no database and no HTTP. Services are tested directly with a session
  (the rollback tests), with no HTTP involved.
- **Reuse.** Phase 3 consumers (RabbitMQ) and Phase 2 stress tests will call the same
  `TransferService`. Business rules must not live in a FastAPI handler.
- **One owner of the transaction boundary.** Routes don't know a transaction exists.
- **Errors stay transport-agnostic.** Services raise `DomainError` subclasses with a stable
  `code`, and only `api/error_handlers.py` maps them to HTTP statuses.

## Low-level design decisions

| Decision | Simpler alternative | More complex alternative | Why this one |
|---|---|---|---|
| Thin repository classes | Queries inline in services | Generic `Repository[T]` + Unit-of-Work interfaces | Keeps SQL out of business code and enforces "no commit here" without abstract base classes we don't need yet |
| `create_app(settings)` factory with session factory on `app.state` | Module-level global engine | DI container library | Tests build an app pointing at the test DB with no monkeypatching or dependency overrides |
| Pure `transfer_rules` module | Rules inside service method | Strategy/policy objects | Unit-testable now; easy to extend in Phase 4 (fraud rules) |
| Two-step validation (pre-load, post-load) | Validate everything after loading | — | Same-account and amount checks don't need a DB round trip |
| Domain errors map to HTTP in one table | `HTTPException` raised from services | Error-code registry | Services stay HTTP-free; status policy lives in one place |
| `VARCHAR` + `CHECK` for enums | Free text | Native PG `ENUM` | Validated, and easy to extend without `ALTER TYPE` |
| 409 for insufficient funds, 422 for invalid input | 400 for everything | — | 409 means "valid request, conflicts with current state"; clients can tell retryable from malformed |
| `expire_on_commit=False` | Default (expire) | — | Returning the committed object doesn't trigger a surprise SELECT per attribute |
| Dev endpoint behind a flag, off by default, refused in production | Always on | Separate admin service with auth | Safe by default with minimal machinery |

## What is NOT solved yet

### 1. Concurrent updates (the big one)

Today a transfer does **read → check → write** without locks:

```
T1: read  source.balance = 100          T2: read  source.balance = 100
T1: check 100 >= 80 ✓                   T2: check 100 >= 80 ✓
T1: write balance = 20, COMMIT          T2: write balance = 20, COMMIT   ← lost update
```

The ORM writes `UPDATE … SET balance_minor = 20` (an absolute value), so T2 silently
overwrites T1. Both transfers are recorded and $160 left the account, but its balance
only dropped by $80. **Money was created.** The CHECK constraint does not help here because
20 ≥ 0. Phase 2 fixes this with `SELECT … FOR UPDATE` on both accounts (in a consistent
order, to avoid deadlocks), or with conditional atomic updates
(`UPDATE … SET balance = balance - :amt WHERE balance >= :amt`), plus concurrent tests.

### 2. Idempotency

If a client's request times out after the server committed, the client can't tell
whether the transfer happened. Retrying creates a **duplicate transfer**. Phase 2 adds
an `Idempotency-Key` header stored with a unique constraint, so a retry returns the original
result.

### 3. Auditability

The balance is a mutable column. There is no double-entry ledger (`ledger_entries`)
to rebuild or reconcile balances from. That arrives with Phase 2.

### 4. Other gaps

There is no authentication, no rate limiting (Phase 2/3), no async event publication (the
outbox pattern comes in Phase 3), and offset pagination degrades on deep pages (keyset
pagination is a later improvement).
