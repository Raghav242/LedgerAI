"""The least-privilege runtime role against the real test database (Phase 6B).

The rest of the integration suite already runs the application as this role (conftest),
which proves it has every privilege it needs. These tests prove what it must NOT be
able to do, and that the policy check fails loudly instead of drifting.
"""

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import ProgrammingError

from app.core.config import Settings
from app.core.db_roles import (
    TABLE_PRIVILEGES,
    RoleSetupError,
    apply_runtime_role,
    effective_privileges,
    verify,
)
from tests.integration.conftest import TEST_RUNTIME_ROLE, apply_test_runtime_role
from tests.integration.support import create_account, transfer


@pytest.fixture
def runtime_engine(test_settings: Settings) -> Engine:
    engine = create_engine(test_settings.database_url)
    yield engine  # type: ignore[misc]
    engine.dispose()


def test_effective_privileges_are_exactly_the_policy(engine: Engine) -> None:
    with engine.connect() as conn:
        actual = effective_privileges(conn, TEST_RUNTIME_ROLE)
    assert actual == {table: set(privs) for table, privs in TABLE_PRIVILEGES.items()}


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE ledger_entries SET amount_minor = amount_minor",
        "DELETE FROM ledger_entries",
        "UPDATE transfers SET amount_minor = amount_minor",
        "DELETE FROM transfers",
        "DELETE FROM event_audit_log",
        "UPDATE processed_events SET consumer_name = consumer_name",
        "DELETE FROM anomaly_results",
        "DELETE FROM accounts",
        "DELETE FROM idempotency_records",
        "DELETE FROM outbox_events",
        "TRUNCATE TABLE lab_runs CASCADE",
        "UPDATE alembic_version SET version_num = version_num",
        "CREATE TABLE runtime_should_not_create (id int)",
        "ALTER TABLE accounts ADD COLUMN nope int",
        "DROP TABLE worker_heartbeats",
    ],
)
def test_runtime_role_cannot_rewrite_history_or_change_the_schema(
    runtime_engine: Engine, session_factory, statement: str
) -> None:  # type: ignore[no-untyped-def]
    source = create_account(session_factory, funded=500)
    destination = create_account(session_factory)
    transfer(session_factory, source, destination, 100)
    with (
        pytest.raises(ProgrammingError, match=r"permission denied|must be owner"),
        runtime_engine.begin() as conn,
    ):
        conn.execute(text(statement))


def test_runtime_role_may_prune_only_lab_and_telemetry_rows(runtime_engine: Engine) -> None:
    with runtime_engine.begin() as conn:
        for table in ("lab_run_events", "lab_runs", "worker_heartbeats"):
            conn.execute(text(f"DELETE FROM {table} WHERE false"))


def test_a_table_without_a_policy_fails_the_role_setup(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE unreviewed_table (id int)"))
    try:
        with engine.begin() as conn, pytest.raises(RoleSetupError, match="unreviewed_table"):
            apply_runtime_role(conn, role=TEST_RUNTIME_ROLE, password="x" * 20, database="x")
    finally:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE unreviewed_table"))
        apply_test_runtime_role(engine)


def test_privilege_drift_is_detected_and_repaired(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(f"GRANT DELETE ON ledger_entries TO {TEST_RUNTIME_ROLE}"))
    with engine.connect() as conn, pytest.raises(RoleSetupError, match="ledger_entries"):
        verify(conn, TEST_RUNTIME_ROLE)
    apply_test_runtime_role(engine)  # re-running the setup removes the excess grant
    with engine.connect() as conn:
        verify(conn, TEST_RUNTIME_ROLE)


def test_runtime_role_has_no_elevated_attributes(engine: Engine) -> None:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls, rolcanlogin "
                "FROM pg_roles WHERE rolname = :r"
            ),
            {"r": TEST_RUNTIME_ROLE},
        ).one()
    assert tuple(row) == (False, False, False, False, True)
