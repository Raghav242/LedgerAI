"""Retention for lab bookkeeping and telemetry (Phase 6B). Never touches money or audit.

    python -m app.maintenance.retention [--dry-run]
    docker compose --profile maintenance run --rm retention [--dry-run]

What is pruned (and only this):
  lab_runs (+ lab_run_events by ON DELETE CASCADE)
      FINISHED runs (never queued/running ones) older than RETENTION_LAB_RUN_DAYS. They hold
      experiment timelines, not money: a lab run's transfers live in `transfers` and the
      ledger like any other and are kept.
  worker_heartbeats
      rows not refreshed for RETENTION_HEARTBEAT_DAYS: instances that no longer exist
      (each restart of a worker container writes a new instance row).

What is kept forever by design: accounts, transfers, ledger_entries, idempotency_records,
outbox_events, processed_events, event_audit_log, anomaly_results.

Two independent safety layers: this module only ever names the tables above, and it runs
as the least-privilege runtime role, which has DELETE on nothing else
(app/core/db_roles.py), so even a bug here could not delete financial or audit rows.
Deletes run in bounded batches (short transactions, no long locks on live tables).
"""

import argparse
import logging
import sys
from dataclasses import dataclass

from sqlalchemy import Engine, text

from app.core.config import Settings
from app.core.database import create_db_engine
from app.core.logging import configure_logging

logger = logging.getLogger("ledgerflow.retention")

BATCH_SIZE = 500


@dataclass(frozen=True, slots=True)
class Policy:
    name: str
    table: str
    count_sql: str
    delete_sql: str  # deletes at most :batch rows per execution


POLICIES = (
    Policy(
        name="finished lab runs (and their timelines)",
        table="lab_runs",
        count_sql=(
            "SELECT count(*) FROM lab_runs WHERE status NOT IN ('queued', 'running') "
            "AND coalesce(finished_at, created_at) < now() - make_interval(days => :lab_days)"
        ),
        delete_sql=(
            "DELETE FROM lab_runs WHERE id IN (SELECT id FROM lab_runs "
            "WHERE status NOT IN ('queued', 'running') "
            "AND coalesce(finished_at, created_at) < now() - make_interval(days => :lab_days) "
            "LIMIT :batch)"
        ),
    ),
    Policy(
        name="stale worker heartbeat rows",
        table="worker_heartbeats",
        count_sql=(
            "SELECT count(*) FROM worker_heartbeats "
            "WHERE beat_at < now() - make_interval(days => :heartbeat_days)"
        ),
        delete_sql=(
            "DELETE FROM worker_heartbeats WHERE (component, instance) IN ("
            "SELECT component, instance FROM worker_heartbeats "
            "WHERE beat_at < now() - make_interval(days => :heartbeat_days) LIMIT :batch)"
        ),
    ),
)
PROTECTED_TABLES = frozenset(
    {
        "accounts",
        "transfers",
        "ledger_entries",
        "idempotency_records",
        "outbox_events",
        "processed_events",
        "event_audit_log",
        "anomaly_results",
    }
)


def run(engine: Engine, settings: Settings, *, dry_run: bool) -> dict[str, int]:
    """Apply every policy; returns rows deleted (or, with dry_run, rows that would be)."""
    params = {
        "lab_days": settings.retention_lab_run_days,
        "heartbeat_days": settings.retention_heartbeat_days,
        "batch": BATCH_SIZE,
    }
    result: dict[str, int] = {}
    for policy in POLICIES:
        assert policy.table not in PROTECTED_TABLES  # defence in depth
        if dry_run:
            with engine.connect() as conn:
                result[policy.table] = conn.execute(text(policy.count_sql), params).scalar_one()
            continue
        deleted = 0
        while True:
            with engine.begin() as conn:
                n = conn.execute(text(policy.delete_sql), params).rowcount
            deleted += n
            if n < BATCH_SIZE:
                break
        result[policy.table] = deleted
        logger.info("Retention: %s: %d row(s) deleted", policy.name, deleted)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="count, delete nothing")
    args = parser.parse_args(argv)
    settings = Settings()  # type: ignore[call-arg]
    configure_logging(settings.log_level)
    engine = create_db_engine(settings)
    try:
        result = run(engine, settings, dry_run=args.dry_run)
    finally:
        engine.dispose()
    verb = "would delete" if args.dry_run else "deleted"
    for table, n in result.items():
        print(f"{table}: {verb} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
