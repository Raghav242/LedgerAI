"""Integration fixtures: a real PostgreSQL *test* database, never the development one.

Session start: create the test database if missing, then run Alembic downgrade->upgrade
so every run exercises the real migrations against a clean schema.
Per test: TRUNCATE all tables so tests are independent.
"""

import re
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.database import CONNECT_TIMEOUT_SECONDS, create_db_engine, create_session_factory
from app.main import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAFE_DB_NAME = re.compile(r"^[a-z_][a-z0-9_]*_test$")
UNLIMITED_RATE_LIMITS = {
    f"rate_limit_{kind}_{field}": value
    for kind in ("read", "write")
    for field, value in (
        ("capacity", 100_000),
        ("refill_tokens", 100_000),
        ("refill_interval_ms", 1),
    )
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/integration" in item.nodeid.replace("\\", "/"):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    base = Settings()  # type: ignore[call-arg]
    test_db = base.postgres_test_db
    # Hard safety guard: refuse to run destructive fixtures against anything that
    # is not clearly a dedicated test database.
    if not SAFE_DB_NAME.match(test_db) or test_db == base.postgres_db:
        pytest.exit(
            f"Refusing to run integration tests: POSTGRES_TEST_DB={test_db!r} must end with "
            f"'_test' and differ from POSTGRES_DB={base.postgres_db!r}",
            returncode=2,
        )
    return base.model_copy(
        update={
            "postgres_db": test_db,
            "app_env": "test",
            "enable_dev_endpoints": True,
            # Business-logic tests are not about rate limiting: use a per-process limiter
            # with the largest allowed quota so it never interferes. Rate-limit tests
            # build their own apps with a real Redis limiter (tests/integration/test_rate_limit*).
            "rate_limit_backend": "memory",
            **UNLIMITED_RATE_LIMITS,
        }
    )


def _ensure_database_exists(settings: Settings) -> None:
    admin_engine = create_engine(
        settings.build_database_url("postgres"),
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
    )
    try:
        with admin_engine.connect() as conn:
            exists = conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": settings.postgres_db},
            )
            if not exists:
                # Name is validated by SAFE_DB_NAME, so quoting it here is safe.
                conn.execute(text(f'CREATE DATABASE "{settings.postgres_db}"'))
    except OperationalError as exc:
        pytest.exit(
            f"Cannot reach PostgreSQL at {settings.postgres_host}:{settings.postgres_port}. "
            f"Is it running? (docker compose up -d postgres)\n{exc}",
            returncode=2,
        )
    finally:
        admin_engine.dispose()


def _alembic_config(settings: Settings) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["database_url"] = settings.database_url
    return config


@pytest.fixture(scope="session")
def alembic_config(test_settings: Settings) -> Config:
    return _alembic_config(test_settings)


@pytest.fixture(scope="session")
def engine(test_settings: Settings, alembic_config: Config) -> Iterator[Engine]:
    _ensure_database_exists(test_settings)
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")
    engine = create_db_engine(test_settings)
    assert engine.url.database == test_settings.postgres_test_db
    yield engine
    engine.dispose()


def reset_database(engine: Engine) -> None:
    """Remove all test data but keep the migration-seeded system funding account.

    TRUNCATE bypasses the append-only row triggers on transfers/ledger_entries, which is
    exactly what test cleanup needs and what the application can never do.
    """
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE idempotency_records, ledger_entries, transfers"))
        conn.execute(text("DELETE FROM accounts WHERE kind <> 'system'"))
        conn.execute(text("UPDATE accounts SET balance_minor = 0 WHERE kind = 'system'"))


@pytest.fixture(autouse=True)
def _clean_tables(engine: Engine) -> Iterator[None]:
    yield
    reset_database(engine)


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


# Concurrency tests run up to 100 threads. Each thread opens its own session; the pool
# caps real connections at 25 (PostgreSQL's default max_connections is 100), so excess
# threads queue for a connection (pool_timeout) instead of failing. Pool queueing is not
# lock contention and never produces ConcurrencyConflictError.
CONCURRENT_POOL_SIZE = 25


@pytest.fixture(scope="session")
def concurrent_engine(engine: Engine, test_settings: Settings) -> Iterator[Engine]:
    concurrent = create_engine(
        test_settings.database_url,
        pool_size=CONCURRENT_POOL_SIZE,
        max_overflow=0,
        pool_timeout=120,
        pool_pre_ping=True,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
    )
    yield concurrent
    concurrent.dispose()


@pytest.fixture
def concurrent_factory(concurrent_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(concurrent_engine)


@pytest.fixture
def client(test_settings: Settings, engine: Engine) -> Iterator[TestClient]:
    app = create_app(test_settings)
    with TestClient(app) as test_client:
        yield test_client


# ---- small API helpers shared by the HTTP tests ---------------------------------


def create_account(client: TestClient, name: str = "Merchant") -> dict:
    response = client.post("/api/v1/accounts", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


def fund_account(client: TestClient, account_id: str, amount_minor: int) -> dict:
    response = client.post(
        f"/api/v1/accounts/{account_id}/fund", json={"amount_minor": amount_minor}
    )
    assert response.status_code == 200, response.text
    return response.json()


def get_balance(client: TestClient, account_id: str) -> int:
    response = client.get(f"/api/v1/accounts/{account_id}")
    assert response.status_code == 200, response.text
    return response.json()["balance_minor"]


def post_transfer(
    client: TestClient,
    source_id: str,
    destination_id: str,
    amount_minor: int,
    *,
    idempotency_key: str | None = None,
):  # -> httpx.Response
    return client.post(
        "/api/v1/transfers",
        json={
            "source_account_id": source_id,
            "destination_account_id": destination_id,
            "amount_minor": amount_minor,
            "currency": "USD",
        },
        headers={"Idempotency-Key": idempotency_key or str(uuid.uuid4())},
    )
