"""double-entry ledger, idempotency records, system funding account

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-11

Legacy data strategy (Phase 1 -> Phase 2)
------------------------------------------
Phase 1 had two kinds of balance changes:

* Transfers: persisted as immutable `transfers` rows (source, destination, amount,
  timestamp). These are backfilled EXACTLY: one debit and one credit entry per transfer,
  stamped with the transfer's own created_at. Nothing is invented.

* Dev funding: only incremented `accounts.balance_minor`; no record was kept. We cannot
  reconstruct individual funding events (amounts, times, count), and we do not fabricate
  them. Instead each account's unexplained residual

      residual = balance - (incoming transfers) + (outgoing transfers)

  is recorded as ONE explicitly labelled movement of kind 'legacy_opening_balance' from
  the system funding account, timestamped at migration time. The label says exactly what
  it is: "this much money existed with no ledger history when the ledger was introduced".

  Phase 1 funding could only ever add money, so a legitimate residual is >= 0. A negative
  residual means the data cannot be explained (e.g. a Phase 1 lost update) and the
  migration ABORTS rather than silently booking a bogus correction. A lost update that
  made a balance *smaller* is not detectable from the data and would be absorbed into a
  smaller opening balance; this limitation is documented in docs/phase-2-design.md.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Hardcoded on purpose (migrations must not import app code). Matches
# app.models.account.SYSTEM_FUNDING_ACCOUNT_ID.
SYSTEM_FUNDING_ACCOUNT_ID = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    # --- accounts: kind + system-account exemption from the non-negative CHECK -------
    op.add_column(
        "accounts",
        sa.Column("kind", sa.String(length=20), server_default=sa.text("'merchant'"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_accounts_kind_valid"), "accounts", "kind IN ('merchant', 'system')"
    )
    op.drop_constraint(op.f("ck_accounts_balance_non_negative"), "accounts", type_="check")
    op.create_check_constraint(
        op.f("ck_accounts_balance_non_negative"),
        "accounts",
        "balance_minor >= 0 OR kind = 'system'",
    )

    # --- transfers: kind ------------------------------------------------------------
    op.add_column(
        "transfers",
        sa.Column("kind", sa.String(length=30), server_default=sa.text("'transfer'"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_transfers_kind_valid"),
        "transfers",
        "kind IN ('transfer', 'dev_funding', 'legacy_opening_balance')",
    )

    # --- ledger_entries -------------------------------------------------------------
    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("transfer_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=10), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("role IN ('debit', 'credit')", name=op.f("ck_ledger_entries_role_valid")),
        sa.CheckConstraint(
            "(role = 'debit' AND amount_minor < 0) OR (role = 'credit' AND amount_minor > 0)",
            name=op.f("ck_ledger_entries_sign_matches_role"),
        ),
        sa.CheckConstraint(
            "currency IN ('USD')", name=op.f("ck_ledger_entries_currency_supported")
        ),
        sa.ForeignKeyConstraint(
            ["transfer_id"],
            ["transfers.id"],
            name=op.f("fk_ledger_entries_transfer_id_transfers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_ledger_entries_account_id_accounts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ledger_entries")),
        sa.UniqueConstraint("transfer_id", "role", name="uq_ledger_entries_transfer_id_role"),
    )
    op.create_index(op.f("ix_ledger_entries_account_id"), "ledger_entries", ["account_id"])

    # --- idempotency_records ----------------------------------------------------------
    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.String(length=100), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=True),
        sa.Column("error_code", sa.String(length=50), nullable=True),
        sa.Column("transfer_id", sa.Uuid(), nullable=True),
        sa.Column("response_body", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "outcome IN ('succeeded', 'rejected')", name=op.f("ck_idempotency_records_outcome_valid")
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR response_body IS NOT NULL",
            name=op.f("ck_idempotency_records_completed_has_response"),
        ),
        sa.CheckConstraint(
            "outcome IS DISTINCT FROM 'succeeded' OR transfer_id IS NOT NULL",
            name=op.f("ck_idempotency_records_success_has_transfer"),
        ),
        sa.CheckConstraint(
            "outcome IS DISTINCT FROM 'rejected' OR error_code IS NOT NULL",
            name=op.f("ck_idempotency_records_rejection_has_error_code"),
        ),
        sa.ForeignKeyConstraint(
            ["transfer_id"],
            ["transfers.id"],
            name=op.f("fk_idempotency_records_transfer_id_transfers"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_idempotency_records")),
        sa.UniqueConstraint(
            "scope", "idempotency_key", name="uq_idempotency_records_scope_key"
        ),
    )

    # --- system funding account ----------------------------------------------------
    op.execute(
        sa.text(
            "INSERT INTO accounts (id, name, currency, kind, balance_minor) "
            "VALUES (CAST(:id AS uuid), 'System: virtual funding source', 'USD', 'system', 0)"
        ).bindparams(id=SYSTEM_FUNDING_ACCOUNT_ID)
    )

    _backfill_legacy_ledger()

    # --- database-enforced ledger invariants -----------------------------------------
    # Created last: PostgreSQL forbids ALTER TABLE on a table with pending deferred
    # trigger events, and the backfill above is already balanced by construction.
    op.execute(LEDGER_FUNCTIONS_SQL)
    op.execute(LEDGER_TRIGGERS_SQL)


def _backfill_legacy_ledger() -> None:
    bind = op.get_bind()

    # 1. Exact backfill of every existing Phase 1 transfer.
    bind.execute(
        sa.text(
            """
            INSERT INTO ledger_entries (id, transfer_id, account_id, role, amount_minor,
                                        currency, created_at)
            SELECT gen_random_uuid(), t.id, t.source_account_id, 'debit', -t.amount_minor,
                   t.currency, t.created_at
            FROM transfers t
            UNION ALL
            SELECT gen_random_uuid(), t.id, t.destination_account_id, 'credit', t.amount_minor,
                   t.currency, t.created_at
            FROM transfers t
            """
        )
    )

    # 2. Residual = money with no ledger history (Phase 1 dev funding).
    residuals = bind.execute(
        sa.text(
            """
            SELECT a.id, a.balance_minor, a.balance_minor - coalesce(sum(e.amount_minor), 0)
            FROM accounts a
            LEFT JOIN ledger_entries e ON e.account_id = a.id
            WHERE a.kind = 'merchant'
            GROUP BY a.id, a.balance_minor
            ORDER BY a.id
            """
        )
    ).all()

    unexplainable = [(str(row[0]), row[1], row[2]) for row in residuals if row[2] < 0]
    if unexplainable:
        raise RuntimeError(
            "Cannot migrate: these accounts hold LESS money than their transfer history "
            "implies, so their balances cannot be explained by Phase 1 funding "
            f"(account_id, balance_minor, residual_minor): {unexplainable}. "
            "Investigate and correct the data manually; nothing was changed."
        )

    # 3. One labelled opening-balance movement per account with a positive residual.
    total_issued = 0
    for account_id, _balance, residual in residuals:
        if residual == 0:
            continue
        transfer_id = bind.execute(
            sa.text(
                """
                INSERT INTO transfers (id, source_account_id, destination_account_id,
                                       amount_minor, currency, status, kind)
                VALUES (gen_random_uuid(), CAST(:system AS uuid), :account, :amount, 'USD', 'completed',
                        'legacy_opening_balance')
                RETURNING id
                """
            ),
            {"system": SYSTEM_FUNDING_ACCOUNT_ID, "account": account_id, "amount": residual},
        ).scalar_one()
        bind.execute(
            sa.text(
                """
                INSERT INTO ledger_entries (id, transfer_id, account_id, role, amount_minor,
                                            currency)
                VALUES (gen_random_uuid(), :transfer, CAST(:system AS uuid), 'debit', -:amount, 'USD'),
                       (gen_random_uuid(), :transfer, :account, 'credit', :amount, 'USD')
                """
            ),
            {
                "transfer": transfer_id,
                "system": SYSTEM_FUNDING_ACCOUNT_ID,
                "account": account_id,
                "amount": residual,
            },
        )
        total_issued += residual

    bind.execute(
        sa.text("UPDATE accounts SET balance_minor = :balance WHERE id = CAST(:system AS uuid)"),
        {"balance": -total_issued, "system": SYSTEM_FUNDING_ACCOUNT_ID},
    )


LEDGER_FUNCTIONS_SQL = """
-- Every transfer must have exactly one debit and one credit that mirror the transfer
-- row (accounts, amount, currency). Two matching entries with opposite signs => sum = 0.
CREATE FUNCTION ledger_assert_transfer_balanced(p_transfer_id uuid) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    v_entries integer;
    v_sum bigint;
BEGIN
    SELECT count(*), coalesce(sum(amount_minor), 0) INTO v_entries, v_sum
    FROM ledger_entries WHERE transfer_id = p_transfer_id;

    IF v_entries <> 2 OR v_sum <> 0
       OR NOT EXISTS (
           SELECT 1 FROM ledger_entries e JOIN transfers t ON t.id = e.transfer_id
           WHERE e.transfer_id = p_transfer_id AND e.role = 'debit'
             AND e.account_id = t.source_account_id
             AND e.amount_minor = -t.amount_minor AND e.currency = t.currency)
       OR NOT EXISTS (
           SELECT 1 FROM ledger_entries e JOIN transfers t ON t.id = e.transfer_id
           WHERE e.transfer_id = p_transfer_id AND e.role = 'credit'
             AND e.account_id = t.destination_account_id
             AND e.amount_minor = t.amount_minor AND e.currency = t.currency)
    THEN
        RAISE EXCEPTION 'ledger invariant violated: transfer % has % entries summing to %',
            p_transfer_id, v_entries, v_sum
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'ledger_transfer_balanced';
    END IF;
END;
$$;

CREATE FUNCTION ledger_check_transfer_row() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM ledger_assert_transfer_balanced(NEW.id);
    RETURN NULL;
END;
$$;

CREATE FUNCTION ledger_check_entry_row() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM ledger_assert_transfer_balanced(NEW.transfer_id);
    RETURN NULL;
END;
$$;

CREATE FUNCTION ledger_reject_modification() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$;
"""

LEDGER_TRIGGERS_SQL = """
-- DEFERRABLE INITIALLY DEFERRED: checked at COMMIT, once both entries exist.
CREATE CONSTRAINT TRIGGER trg_transfers_balanced
    AFTER INSERT ON transfers
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION ledger_check_transfer_row();

CREATE CONSTRAINT TRIGGER trg_ledger_entries_balanced
    AFTER INSERT ON ledger_entries
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION ledger_check_entry_row();

-- Immutability. (TRUNCATE is still possible for administrators; it is not exposed.)
CREATE TRIGGER trg_ledger_entries_append_only
    BEFORE UPDATE OR DELETE ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION ledger_reject_modification();

CREATE TRIGGER trg_transfers_append_only
    BEFORE UPDATE OR DELETE ON transfers
    FOR EACH ROW EXECUTE FUNCTION ledger_reject_modification();
"""


def downgrade() -> None:
    """Return to the Phase 1 schema without changing any merchant balance.

    Ledger entries and idempotency records are dropped. Movements that only exist in the
    Phase 2 model (dev_funding, legacy_opening_balance: both involve the system account)
    are deleted together with the system account; their effect remains in the merchant
    balances exactly as Phase 1 funding did.
    """
    op.execute("DROP TRIGGER trg_transfers_append_only ON transfers")
    op.execute("DROP TRIGGER trg_ledger_entries_append_only ON ledger_entries")
    op.execute("DROP TRIGGER trg_ledger_entries_balanced ON ledger_entries")
    op.execute("DROP TRIGGER trg_transfers_balanced ON transfers")
    op.execute("DROP FUNCTION ledger_reject_modification()")
    op.execute("DROP FUNCTION ledger_check_entry_row()")
    op.execute("DROP FUNCTION ledger_check_transfer_row()")
    op.execute("DROP FUNCTION ledger_assert_transfer_balanced(uuid)")

    op.drop_table("idempotency_records")
    op.drop_index(op.f("ix_ledger_entries_account_id"), table_name="ledger_entries")
    op.drop_table("ledger_entries")

    op.execute("DELETE FROM transfers WHERE kind <> 'transfer'")
    op.execute(
        sa.text("DELETE FROM accounts WHERE id = CAST(:system AS uuid)").bindparams(system=SYSTEM_FUNDING_ACCOUNT_ID)
    )

    op.drop_constraint(op.f("ck_transfers_kind_valid"), "transfers", type_="check")
    op.drop_column("transfers", "kind")

    op.drop_constraint(op.f("ck_accounts_balance_non_negative"), "accounts", type_="check")
    op.create_check_constraint(
        op.f("ck_accounts_balance_non_negative"), "accounts", "balance_minor >= 0"
    )
    op.drop_constraint(op.f("ck_accounts_kind_valid"), "accounts", type_="check")
    op.drop_column("accounts", "kind")
