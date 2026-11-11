"""Requirement 13: migration 0002 on realistic Phase 1 data.

Each test downgrades the test database to the Phase 1 schema (0001), inserts Phase 1
style rows with raw SQL (the Phase 2 models no longer match that schema), upgrades and
checks the result. The finally-block always restores a clean schema at head.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.support import reconcile

SYSTEM = uuid.UUID("00000000-0000-0000-0000-000000000001")
TRANSFER_TIME = datetime(2026, 11, 3, 2, 5, 36, tzinfo=UTC)


@pytest.fixture
def phase1_database(alembic_config: Config, engine: Engine) -> Iterator[Engine]:
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE accounts, transfers CASCADE"))
    command.downgrade(alembic_config, "0001")
    try:
        yield engine
    finally:
        with engine.begin() as conn:
            conn.execute(text("TRUNCATE TABLE accounts, transfers CASCADE"))
        command.downgrade(alembic_config, "0001")  # no-op if the upgrade aborted
        command.upgrade(alembic_config, "head")  # re-seeds the system account


def _insert_phase1(engine: Engine, balances: dict[str, int], transfers: list[tuple]) -> dict:
    ids = {name: uuid.uuid4() for name in balances}
    with engine.begin() as conn:
        for name, amount in balances.items():
            conn.execute(
                text(
                    "INSERT INTO accounts (id, name, currency, balance_minor) "
                    "VALUES (:id, :name, 'USD', :balance)"
                ),
                {"id": ids[name], "name": name, "balance": amount},
            )
        for source, destination, amount in transfers:
            conn.execute(
                text(
                    "INSERT INTO transfers (id, source_account_id, destination_account_id, "
                    "amount_minor, currency, status, created_at) "
                    "VALUES (:id, :s, :d, :amount, 'USD', 'completed', :at)"
                ),
                {
                    "id": uuid.uuid4(),
                    "s": ids[source],
                    "d": ids[destination],
                    "amount": amount,
                    "at": TRANSFER_TIME,
                },
            )
    return ids


def test_upgrade_backfills_transfers_exactly_and_labels_unrecorded_funding(
    phase1_database: Engine, alembic_config: Config, session_factory: sessionmaker[Session]
) -> None:
    # Mirrors the real Phase 1 dev data: Acme was funded 10000 and paid Bob 2550;
    # Raghav was funded 1050; Dana never received money.
    ids = _insert_phase1(
        phase1_database,
        {"Acme": 7_450, "Bob": 2_550, "Raghav": 1_050, "Dana": 0},
        [("Acme", "Bob", 2_550)],
    )

    command.upgrade(alembic_config, "head")

    with phase1_database.connect() as conn:
        legacy = conn.execute(
            text(
                "SELECT e.account_id, e.role, e.amount_minor, e.created_at FROM ledger_entries e "
                "JOIN transfers t ON t.id = e.transfer_id WHERE t.kind = 'transfer' "
                "ORDER BY e.role DESC"
            )
        ).all()
        openings = dict(
            conn.execute(
                text(
                    "SELECT destination_account_id, amount_minor FROM transfers "
                    "WHERE kind = 'legacy_opening_balance' AND source_account_id = :system"
                ),
                {"system": SYSTEM},
            ).all()
        )
        balances = dict(conn.execute(text("SELECT id, balance_minor FROM accounts")).all())

    # The real transfer: two exact entries, stamped with the transfer's own time.
    assert legacy == [
        (ids["Acme"], "debit", -2_550, TRANSFER_TIME),
        (ids["Bob"], "credit", 2_550, TRANSFER_TIME),
    ]
    # Unrecorded funding: one labelled opening balance per account that needs it.
    assert openings == {ids["Acme"]: 10_000, ids["Raghav"]: 1_050}
    # No merchant balance changed; the system account offsets the issued money.
    assert balances[ids["Acme"]] == 7_450
    assert balances[ids["Bob"]] == 2_550
    assert balances[ids["Raghav"]] == 1_050
    assert balances[ids["Dana"]] == 0
    assert balances[SYSTEM] == -11_050
    assert reconcile(session_factory).ok


def test_upgrade_aborts_on_balances_that_history_cannot_explain(
    phase1_database: Engine, alembic_config: Config
) -> None:
    # Ann holds 500 but received 1000 and never paid anything out: money disappeared
    # (e.g. a Phase 1 lost update). The migration must refuse, not paper over it.
    ids = _insert_phase1(phase1_database, {"Ann": 500, "Ben": 0}, [("Ben", "Ann", 1_000)])

    with pytest.raises(RuntimeError, match=str(ids["Ann"])):
        command.upgrade(alembic_config, "head")

    with phase1_database.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0001"
        assert conn.scalar(text("SELECT to_regclass('ledger_entries')")) is None
        assert (
            conn.scalar(
                text("SELECT balance_minor FROM accounts WHERE id = :id"), {"id": ids["Ann"]}
            )
            == 500
        )


def test_downgrade_preserves_merchant_balances(
    phase1_database: Engine, alembic_config: Config
) -> None:
    ids = _insert_phase1(phase1_database, {"Acme": 7_450, "Bob": 2_550}, [("Acme", "Bob", 2_550)])
    command.upgrade(alembic_config, "head")

    command.downgrade(alembic_config, "0001")

    with phase1_database.connect() as conn:
        balances = dict(conn.execute(text("SELECT id, balance_minor FROM accounts")).all())
        transfer_count = conn.scalar(text("SELECT count(*) FROM transfers"))
    assert balances == {ids["Acme"]: 7_450, ids["Bob"]: 2_550}  # system account removed
    assert transfer_count == 1  # only the real Phase 1 transfer remains
