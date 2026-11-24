"""Least-privilege PostgreSQL runtime role (Phase 6B). Idempotent; run after migrations.

    python -m app.core.db_roles            # the `migrate` Compose job runs it after alembic

Two roles:
  migration role  POSTGRES_USER: owns the schema, runs Alembic (DDL), runs this script
  runtime role    APP_DB_USER:   what the API replicas and workers connect as; LOGIN, no
                  superuser, no CREATEDB/CREATEROLE, cannot create objects, and has exactly
                  the table privileges below

Grants follow what the code does with each table, and make two invariants database-
enforced instead of convention:
  - the ledger and the audit trail are APPEND-ONLY for the application (no UPDATE/DELETE
    on ledger_entries, transfers, event_audit_log, processed_events, anomaly_results)
  - retention can only ever delete lab and telemetry rows (DELETE is granted nowhere else)

A table without a policy here makes the script FAIL, so a new migration cannot silently
leave the runtime role without access (or with too much): it forces a decision.
"""

import logging
import sys

from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import URL

from app.core.config import Settings
from app.core.logging import configure_logging

logger = logging.getLogger("ledgerflow.db_roles")

SELECT, INSERT, UPDATE, DELETE = "SELECT", "INSERT", "UPDATE", "DELETE"

TABLE_PRIVILEGES: dict[str, tuple[str, ...]] = {
    # money: balances change under row locks; the ledger itself is append-only
    "accounts": (SELECT, INSERT, UPDATE),
    "transfers": (SELECT, INSERT),
    "ledger_entries": (SELECT, INSERT),
    "idempotency_records": (SELECT, INSERT, UPDATE),
    # events: outbox rows are claimed and marked published; receipts and audit append-only
    "outbox_events": (SELECT, INSERT, UPDATE),
    "processed_events": (SELECT, INSERT),
    "event_audit_log": (SELECT, INSERT),
    "anomaly_results": (SELECT, INSERT),
    # lab bookkeeping and telemetry: the only tables retention may prune
    "lab_runs": (SELECT, INSERT, UPDATE, DELETE),
    "lab_run_events": (SELECT, INSERT, DELETE),
    "worker_heartbeats": (SELECT, INSERT, UPDATE, DELETE),
    # migration bookkeeping: readable (diagnostics), never writable by the application
    "alembic_version": (SELECT,),
}
ALL_PRIVILEGES = (SELECT, INSERT, UPDATE, DELETE, "TRUNCATE", "REFERENCES", "TRIGGER")


class RoleSetupError(RuntimeError):
    pass


def _ident(conn: Connection, name: str) -> str:
    return conn.execute(text("SELECT quote_ident(:n)"), {"n": name}).scalar_one()


def public_tables(conn: Connection) -> set[str]:
    rows = conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
    return {r[0] for r in rows}


def apply_runtime_role(conn: Connection, *, role: str, password: str, database: str) -> None:
    """Create/update `role` and set exactly the privileges in TABLE_PRIVILEGES."""
    tables = public_tables(conn)
    unknown = sorted(tables - TABLE_PRIVILEGES.keys())
    if unknown:
        raise RoleSetupError(
            f"tables without a runtime privilege policy: {unknown}; add them to "
            "app/core/db_roles.py TABLE_PRIVILEGES"
        )
    r = _ident(conn, role)
    exists = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).first()
    verb = "ALTER" if exists else "CREATE"
    # The password is sent as a bound literal (format('%L')) so it is never interpolated raw.
    statement = conn.execute(
        text(
            "SELECT format('%s ROLE %s LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOREPLICATION NOBYPASSRLS INHERIT PASSWORD %L', CAST(:verb AS text), "
            "CAST(:role AS text), CAST(:pw AS text))"
        ),
        {"verb": verb, "role": r, "pw": password},
    ).scalar_one()
    conn.execute(text(statement))
    conn.execute(text(f"GRANT CONNECT ON DATABASE {_ident(conn, database)} TO {r}"))
    conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {r}"))
    conn.execute(text(f"REVOKE CREATE ON SCHEMA public FROM {r}"))
    for table in sorted(tables):
        t = _ident(conn, table)
        conn.execute(text(f"REVOKE ALL ON TABLE public.{t} FROM {r}"))
        conn.execute(text(f"GRANT {', '.join(TABLE_PRIVILEGES[table])} ON public.{t} TO {r}"))
    conn.execute(text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {r}"))


def effective_privileges(conn: Connection, role: str) -> dict[str, set[str]]:
    """What `role` can actually do per table (has_table_privilege), for verification."""
    out: dict[str, set[str]] = {}
    for table in sorted(public_tables(conn)):
        out[table] = {
            p
            for p in ALL_PRIVILEGES
            if conn.execute(
                text("SELECT has_table_privilege(:r, :t, :p)"),
                {"r": role, "t": f"public.{table}", "p": p},
            ).scalar_one()
        }
    return out


def verify(conn: Connection, role: str) -> None:
    actual = effective_privileges(conn, role)
    wrong = {t: sorted(p) for t, p in actual.items() if p != set(TABLE_PRIVILEGES[t])}
    if wrong:
        raise RoleSetupError(f"runtime role privileges differ from policy: {wrong}")
    flags = conn.execute(
        text(
            "SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolbypassrls "
            "FROM pg_roles WHERE rolname = :r"
        ),
        {"r": role},
    ).scalar_one()
    if flags:
        raise RoleSetupError(f"runtime role {role!r} has elevated attributes")


def main() -> int:
    settings = Settings()  # type: ignore[call-arg]
    configure_logging(settings.log_level)
    if settings.app_db_password is None:
        logger.warning("APP_DB_PASSWORD not set: runtime role not managed (single-role mode)")
        return 0
    if settings.app_db_user == settings.postgres_user:
        logger.error("APP_DB_USER must differ from the migration role POSTGRES_USER")
        return 1
    password = settings.app_db_password.get_secret_value()
    if password == settings.postgres_password.get_secret_value():
        logger.error("APP_DB_PASSWORD must differ from POSTGRES_PASSWORD")
        return 1
    url: URL = settings.database_url  # the MIGRATION role's credentials
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            apply_runtime_role(
                conn, role=settings.app_db_user, password=password, database=settings.postgres_db
            )
            verify(conn, settings.app_db_user)
    except RoleSetupError as exc:
        logger.error("%s", exc)
        return 1
    finally:
        engine.dispose()
    logger.info(
        "Runtime role %r applied on %s: %d tables, ledger and audit tables append-only",
        settings.app_db_user,
        settings.postgres_db,
        len(TABLE_PRIVILEGES),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
