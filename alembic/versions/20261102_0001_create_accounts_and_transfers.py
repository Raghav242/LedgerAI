"""create accounts and transfers

Revision ID: 0001
Revises:
Create Date: 2026-11-02

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("balance_minor", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("balance_minor >= 0", name=op.f("ck_accounts_balance_non_negative")),
        sa.CheckConstraint("currency IN ('USD')", name=op.f("ck_accounts_currency_supported")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounts")),
    )

    op.create_table(
        "transfers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_account_id", sa.Uuid(), nullable=False),
        sa.Column("destination_account_id", sa.Uuid(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount_minor > 0", name=op.f("ck_transfers_amount_positive")),
        sa.CheckConstraint(
            "source_account_id <> destination_account_id",
            name=op.f("ck_transfers_distinct_accounts"),
        ),
        sa.CheckConstraint("currency IN ('USD')", name=op.f("ck_transfers_currency_supported")),
        sa.CheckConstraint("status IN ('completed')", name=op.f("ck_transfers_status_valid")),
        sa.ForeignKeyConstraint(
            ["source_account_id"],
            ["accounts.id"],
            name=op.f("fk_transfers_source_account_id_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["destination_account_id"],
            ["accounts.id"],
            name=op.f("fk_transfers_destination_account_id_accounts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transfers")),
    )
    op.create_index(
        op.f("ix_transfers_source_account_id"), "transfers", ["source_account_id"]
    )
    op.create_index(
        op.f("ix_transfers_destination_account_id"), "transfers", ["destination_account_id"]
    )
    op.create_index(op.f("ix_transfers_created_at"), "transfers", ["created_at"])


def downgrade() -> None:
    op.drop_index(op.f("ix_transfers_created_at"), table_name="transfers")
    op.drop_index(op.f("ix_transfers_destination_account_id"), table_name="transfers")
    op.drop_index(op.f("ix_transfers_source_account_id"), table_name="transfers")
    op.drop_table("transfers")
    op.drop_table("accounts")
