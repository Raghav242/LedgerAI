"""AI anomaly results

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-20

One row per scored transfer.completed event, written by the AI worker in the same
transaction as its processed_events receipt, so a redelivered event can never produce a
second result (event_id is also the primary key). Nothing financial changes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "anomaly_results",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("transfer_id", sa.Uuid(), nullable=False),
        sa.Column("source_account_id", sa.Uuid(), nullable=False),
        sa.Column("model_version", sa.String(length=40), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("is_anomaly", sa.Boolean(), nullable=False),
        sa.Column("features", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("explanation", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rule_flags", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("inference_ms", sa.Float(), nullable=False),
        sa.Column("delivery_attempt", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "scored_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_anomaly_results")),
        sa.UniqueConstraint("transfer_id", name=op.f("uq_anomaly_results_transfer_id")),
    )
    op.create_index("ix_anomaly_results_scored_at", "anomaly_results", ["scored_at"])
    op.create_index("ix_anomaly_results_source_account_id", "anomaly_results", ["source_account_id"])
    # The worker's feature queries: earlier transfers of one source account, by time.
    op.create_index(
        "ix_transfers_source_account_created_at", "transfers", ["source_account_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_transfers_source_account_created_at", table_name="transfers")
    op.drop_index("ix_anomaly_results_source_account_id", table_name="anomaly_results")
    op.drop_index("ix_anomaly_results_scored_at", table_name="anomaly_results")
    op.drop_table("anomaly_results")
