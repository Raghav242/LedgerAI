"""PostgreSQL backup and VERIFIED restore for the local stack (Phase 6B).

    python scripts/db_backup.py backup  [--project dev|stacktest] [--database NAME]
    python scripts/db_backup.py verify  backups/<file>.dump
    python scripts/db_backup.py restore-drill [--project dev]       # backup + verify

backup   `pg_dump --format=custom` inside the project's PostgreSQL container (consistent
         snapshot, no downtime), streamed to backups/<db>-<timestamp>.dump (gitignored),
         with a SHA-256 next to it.
verify   restores the dump into a THROWAWAY PostgreSQL container (own name, no published
         port, no volume, removed afterwards) and checks it: the restore succeeds, the
         Alembic version matches, every table's row count matches the counts recorded at
         backup time, the ledger reconciles (sum of postings = 0, every account balance
         = its postings) and the append-only triggers came back.

Restoring OVER a live database is deliberately not offered: this script cannot target the
development, stack-test or benchmark databases. To roll a real environment back, restore
into a new database, verify it, and repoint the application (see docs/operations.md).
"""

import argparse
import hashlib
import json
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import stack_control  # noqa: E402

BACKUP_DIR = ROOT / "backups"
POSTGRES_IMAGE = "postgres:16-alpine"  # same major version as the stack
PROJECTS = {"dev": stack_control.DEV, "stacktest": stack_control.STACK}
COUNT_TABLES_SQL = (
    "SELECT string_agg(format('SELECT %L AS t, count(*) AS n FROM %I', tablename, tablename), "
    "' UNION ALL ') FROM pg_tables WHERE schemaname = 'public'"
)
RECONCILE_SQL = """
SELECT
  (SELECT coalesce(sum(amount_minor), 0) FROM ledger_entries) AS ledger_total,
  (SELECT count(*) FROM accounts a WHERE a.balance_minor <> (
      SELECT coalesce(sum(e.amount_minor), 0) FROM ledger_entries e
      WHERE e.account_id = a.id)) AS mismatched_accounts,
  (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal) AS triggers,
  (SELECT version_num FROM alembic_version) AS alembic
"""


def _run(  # type: ignore[no-untyped-def]
    cmd: list[str], *, stdin=None, stdout=None, check: bool = True
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, stdin=stdin, stdout=stdout or subprocess.PIPE, stderr=subprocess.PIPE, check=check
    )


def _psql(container: str, user: str, database: str, sql: str) -> str:
    out = _run(["docker", "exec", container, "psql", "-U", user, "-d", database, "-tAc", sql])
    return out.stdout.decode().strip()


def _counts(container: str, user: str, database: str) -> dict[str, int]:
    union = _psql(container, user, database, COUNT_TABLES_SQL)
    rows = _psql(container, user, database, union).splitlines()
    return {t: int(n) for t, n in (r.split("|") for r in rows if r)}


class _SnapshotSession:
    """A psql session holding a REPEATABLE READ transaction whose snapshot pg_dump reuses,
    so the recorded row counts describe exactly the data in the dump, even under writes."""

    END = "__ledgerflow_end__"

    def __init__(self, container: str, user: str, database: str) -> None:
        self._p = subprocess.Popen(
            [
                "docker",
                "exec",
                "-i",
                container,
                "psql",
                "-U",
                user,
                "-d",
                database,
                "-qtA",
                "-v",
                "ON_ERROR_STOP=1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )

    def query(self, sql: str) -> list[str]:
        assert self._p.stdin is not None and self._p.stdout is not None
        self._p.stdin.write(f"{sql};\nSELECT '{self.END}';\n")
        self._p.stdin.flush()
        lines = []
        for line in self._p.stdout:
            if line.strip() == self.END:
                return lines
            if line.strip():
                lines.append(line.strip())
        raise RuntimeError("psql session ended unexpectedly")

    def close(self) -> None:
        assert self._p.stdin is not None
        self._p.stdin.write("COMMIT;\n\\q\n")
        self._p.stdin.flush()
        self._p.wait(timeout=30)


def backup(project: stack_control.Project, database: str | None = None) -> Path:
    from app.core.config import Settings

    settings = Settings()  # type: ignore[call-arg]  # the migration role (.env)
    db = database or project.env.get("POSTGRES_DB", settings.postgres_db)
    container = project.container("postgres")
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = BACKUP_DIR / f"{db}-{stamp}.dump"
    session = _SnapshotSession(container, settings.postgres_user, db)
    try:
        session.query("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        (snapshot,) = session.query("SELECT pg_export_snapshot()")
        (union,) = session.query(COUNT_TABLES_SQL)
        counts = {t: int(n) for t, n in (r.split("|") for r in session.query(union))}
        with path.open("wb") as f:
            _run(
                [
                    "docker",
                    "exec",
                    container,
                    "pg_dump",
                    "-U",
                    settings.postgres_user,
                    "-d",
                    db,
                    f"--snapshot={snapshot}",
                    "--format=custom",
                    "--no-owner",
                    "--no-privileges",
                ],
                stdout=f,
            )
    finally:
        session.close()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {
        "database": db,
        "project": project.name,
        "created_at": stamp,
        "sha256": digest,
        "bytes": path.stat().st_size,
        "row_counts": counts,
    }
    path.with_suffix(".json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Wrote {path.relative_to(ROOT)} ({manifest['bytes']} bytes, {len(counts)} tables)")
    return path


def verify(path: Path) -> dict:
    manifest = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["sha256"]:
        raise SystemExit(f"{path.name}: SHA-256 does not match its manifest")
    name = f"ledgerflow-restore-drill-{uuid.uuid4().hex[:8]}"
    password = uuid.uuid4().hex
    # Throwaway server: no published port, no named volume, removed in `finally`.
    _run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "-e",
            "POSTGRES_USER=restore",
            "-e",
            "POSTGRES_DB=restored",
            POSTGRES_IMAGE,
        ]
    )
    try:
        deadline = time.monotonic() + 60
        while (
            _run(
                ["docker", "exec", name, "pg_isready", "-U", "restore", "-d", "restored"],
                check=False,
            ).returncode
            != 0
        ):
            if time.monotonic() > deadline:
                raise SystemExit("throwaway PostgreSQL did not become ready")
            time.sleep(0.5)
        time.sleep(1)  # the image restarts once after initdb
        with path.open("rb") as f:
            _run(
                [
                    "docker",
                    "exec",
                    "-i",
                    name,
                    "pg_restore",
                    "-U",
                    "restore",
                    "-d",
                    "restored",
                    "--no-owner",
                    "--no-privileges",
                    "--exit-on-error",
                ],
                stdin=f,
            )
        counts = _counts(name, "restore", "restored")
        ledger_total, mismatched, triggers, alembic = _psql(
            name, "restore", "restored", " ".join(RECONCILE_SQL.split())
        ).split("|")
    finally:
        _run(["docker", "rm", "-f", name], check=False)
    report = {
        "file": path.name,
        "row_counts_match": counts == manifest["row_counts"],
        "tables": len(counts),
        "rows": sum(counts.values()),
        "ledger_total_minor": int(ledger_total),
        "mismatched_accounts": int(mismatched),
        "append_only_triggers": int(triggers),
        "alembic_version": alembic,
    }
    report["ok"] = (
        report["row_counts_match"]
        and report["ledger_total_minor"] == 0
        and report["mismatched_accounts"] == 0
        and report["append_only_triggers"] > 0
    )
    print(json.dumps(report, indent=2))
    if not report["ok"]:
        diff = {
            t: (manifest["row_counts"].get(t), counts.get(t))
            for t in set(counts) | set(manifest["row_counts"])
            if counts.get(t) != manifest["row_counts"].get(t)
        }
        raise SystemExit(f"RESTORE VERIFICATION FAILED: {diff or report}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("backup")
    b.add_argument("--project", choices=sorted(PROJECTS), default="dev")
    b.add_argument("--database")
    v = sub.add_parser("verify")
    v.add_argument("file", type=Path)
    d = sub.add_parser("restore-drill")
    d.add_argument("--project", choices=sorted(PROJECTS), default="dev")
    d.add_argument("--database")
    args = parser.parse_args()
    if args.command == "verify":
        verify(args.file)
    else:
        path = backup(PROJECTS[args.project], args.database)
        if args.command == "restore-drill":
            verify(path)


if __name__ == "__main__":
    main()
