"""experiment lab runs and their event timelines

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-18

Only new tables; nothing financial is touched. Lab runs are development bookkeeping:
their accounts and transfers live in the normal tables like any other client's.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "lab_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("experiment", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("progress", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("checks", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("runner_replica", sa.String(length=100), nullable=False),
        sa.Column("target_base_url", sa.String(length=200), nullable=False),
        sa.Column("event_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("events_dropped", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "heartbeat_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'passed', 'failed', 'error')",
            name=op.f("ck_lab_runs_status_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lab_runs")),
    )
    op.create_index("ix_lab_runs_created_at", "lab_runs", ["created_at"])
    # At most ONE active run, enforced by the database across every replica: a unique
    # index over a constant, restricted to active rows.
    op.execute(
        "CREATE UNIQUE INDEX uq_lab_runs_one_active ON lab_runs ((true)) "
        "WHERE status IN ('queued', 'running')"
    )
    op.create_table(
        "lab_run_events",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["lab_runs.id"],
            name=op.f("fk_lab_run_events_run_id_lab_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "seq", name=op.f("pk_lab_run_events")),
    )


def downgrade() -> None:
    op.drop_table("lab_run_events")
    op.execute("DROP INDEX IF EXISTS uq_lab_runs_one_active")
    op.drop_index("ix_lab_runs_created_at", table_name="lab_runs")
    op.drop_table("lab_runs")
