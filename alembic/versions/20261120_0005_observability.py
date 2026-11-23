"""observability: worker heartbeats and trace context on outbox events

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-20

Nothing financial changes. `worker_heartbeats` lets the API report whether the background
workers are actually making progress (a running container is not proof of that), and
`outbox_events.trace_parent` carries the W3C trace context of the request that committed
the event, so the asynchronous half of a transfer can join the same distributed trace.
The column is nullable: tracing is optional and older rows have no context.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "worker_heartbeats",
        sa.Column("component", sa.String(length=60), nullable=False),
        sa.Column("instance", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "beat_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("next_beat_before", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('ok', 'degraded', 'error')", name=op.f("ck_worker_heartbeats_status_valid")
        ),
        sa.PrimaryKeyConstraint("component", "instance", name=op.f("pk_worker_heartbeats")),
    )
    op.add_column("outbox_events", sa.Column("trace_parent", sa.String(length=64), nullable=True))
    # Event Pipeline page: newest-first pages of events (keyset on created_at, id).
    op.create_index("ix_outbox_events_created_at", "outbox_events", ["created_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_outbox_events_created_at", table_name="outbox_events")
    op.drop_column("outbox_events", "trace_parent")
    op.drop_table("worker_heartbeats")
