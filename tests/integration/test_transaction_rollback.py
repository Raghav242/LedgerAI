"""Atomicity tests: a failure at any point inside a transfer must leave no trace.

These call the service directly (no HTTP) and inject failures *after* the debit has
been applied in memory, which is exactly the moment a partial write would be possible.
"""

import uuid

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import Account, Transfer
from app.repositories.transfer_repository import TransferRepository
from app.services import transfer_service as transfer_service_module
from app.services.account_service import AccountService
from app.services.transfer_service import TransferService


def _setup_accounts(factory: sessionmaker[Session], funded: int) -> tuple[uuid.UUID, uuid.UUID]:
    with factory() as session:
        service = AccountService(session)
        source = service.create_account("Source", "USD")
        destination = service.create_account("Destination", "USD")
        if funded:
            service.fund_account(source.id, funded)
        return source.id, destination.id


def _snapshot(factory: sessionmaker[Session], *ids: uuid.UUID) -> tuple[list[int], int]:
    """Read committed state through a brand-new session (no cached objects)."""
    with factory() as session:
        balances = [session.get_one(Account, i).balance_minor for i in ids]
        transfer_count = session.scalar(select(func.count()).select_from(Transfer))
        return balances, transfer_count or 0


def test_failure_after_debit_rolls_back_everything(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    source_id, destination_id = _setup_accounts(session_factory, funded=1_000)

    def explode(self: TransferRepository, transfer: Transfer) -> Transfer:
        raise RuntimeError("simulated crash while writing the transfer record")

    # By the time add() runs, both balances have already been modified in the session.
    monkeypatch.setattr(TransferRepository, "add", explode)

    with session_factory() as session, pytest.raises(RuntimeError):
        TransferService(session).create_transfer(
            source_account_id=source_id,
            destination_account_id=destination_id,
            amount_minor=400,
            currency="USD",
        )

    assert _snapshot(session_factory, source_id, destination_id) == ([1_000, 0], 0)


def test_database_check_constraint_rolls_back_whole_transfer(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defense in depth: even if the application's funds check were buggy, the
    CHECK (balance_minor >= 0) constraint rejects the overdraft and the transaction
    rolls back the credit and the transfer row with it."""
    source_id, destination_id = _setup_accounts(session_factory, funded=100)
    monkeypatch.setattr(
        transfer_service_module, "validate_transfer_against_accounts", lambda *a, **k: None
    )

    with session_factory() as session, pytest.raises(IntegrityError) as exc_info:
        TransferService(session).create_transfer(
            source_account_id=source_id,
            destination_account_id=destination_id,
            amount_minor=500,
            currency="USD",
        )

    assert "ck_accounts_balance_non_negative" in str(exc_info.value)
    assert _snapshot(session_factory, source_id, destination_id) == ([100, 0], 0)


def test_session_is_usable_after_a_rolled_back_transfer(
    session_factory: sessionmaker[Session],
) -> None:
    source_id, destination_id = _setup_accounts(session_factory, funded=1_000)

    with session_factory() as session:
        service = TransferService(session)
        with pytest.raises(Exception, match="insufficient"):
            service.create_transfer(
                source_account_id=source_id,
                destination_account_id=destination_id,
                amount_minor=5_000,
                currency="USD",
            )
        service.create_transfer(
            source_account_id=source_id,
            destination_account_id=destination_id,
            amount_minor=300,
            currency="USD",
        )

    assert _snapshot(session_factory, source_id, destination_id) == ([700, 300], 1)


def test_negative_balance_is_rejected_by_database(session_factory: sessionmaker[Session]) -> None:
    source_id, _ = _setup_accounts(session_factory, funded=0)
    with session_factory() as session, pytest.raises(IntegrityError):
        session.execute(update(Account).where(Account.id == source_id).values(balance_minor=-1))
        session.commit()


@pytest.mark.parametrize(
    ("column_sql", "constraint"),
    [
        ("amount_minor = 0", "ck_transfers_amount_positive"),
        ("destination_account_id = source_account_id", "ck_transfers_distinct_accounts"),
    ],
)
def test_transfer_table_constraints(
    session_factory: sessionmaker[Session], column_sql: str, constraint: str
) -> None:
    source_id, destination_id = _setup_accounts(session_factory, funded=0)
    with session_factory() as session:
        session.execute(
            text(
                "INSERT INTO transfers (id, source_account_id, destination_account_id, "
                "amount_minor, currency, status) VALUES (:id, :s, :d, 1, 'USD', 'completed')"
            ),
            {"id": uuid.uuid4(), "s": source_id, "d": destination_id},
        )
        with pytest.raises(IntegrityError) as exc_info:
            session.execute(text(f"UPDATE transfers SET {column_sql}"))
        assert constraint in str(exc_info.value)
