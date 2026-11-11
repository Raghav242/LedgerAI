import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import SYSTEM_FUNDING_ACCOUNT_ID, LedgerEntry, Transfer, TransferKind
from app.repositories.ledger_repository import LedgerRepository
from tests.integration.conftest import create_account as api_create_account
from tests.integration.conftest import fund_account as api_fund_account
from tests.integration.conftest import post_transfer
from tests.integration.support import (
    create_account,
    ledger_entry_count,
    reconcile,
    transfer,
)

Factory = sessionmaker[Session]


def _entries(factory: Factory, transfer_id: uuid.UUID) -> list[LedgerEntry]:
    with factory() as session:
        return LedgerRepository(session).entries_for_transfer(transfer_id)


def test_successful_transfer_has_exactly_two_balanced_entries(session_factory: Factory) -> None:
    """Requirement 9."""
    source = create_account(session_factory, funded=1_000)
    destination = create_account(session_factory)

    result = transfer(session_factory, source, destination, 375)
    assert result.transfer_id is not None
    debit, credit = _entries(session_factory, result.transfer_id)

    assert (debit.role, debit.account_id, debit.amount_minor) == ("debit", source, -375)
    assert (credit.role, credit.account_id, credit.amount_minor) == ("credit", destination, 375)
    assert debit.amount_minor + credit.amount_minor == 0
    assert debit.currency == credit.currency == "USD"


def test_funding_is_a_balanced_posting_from_the_system_account(session_factory: Factory) -> None:
    account = create_account(session_factory, funded=2_000)

    with session_factory() as session:
        funding = session.scalars(
            select(Transfer).where(Transfer.kind == TransferKind.DEV_FUNDING)
        ).one()
    debit, credit = _entries(session_factory, funding.id)

    assert (debit.account_id, debit.amount_minor) == (SYSTEM_FUNDING_ACCOUNT_ID, -2_000)
    assert (credit.account_id, credit.amount_minor) == (account, 2_000)
    report = reconcile(session_factory)
    assert report.ok
    assert report.balance_total_minor == 0  # system -2000 + merchant 2000


def test_rejected_transfers_write_no_ledger_entries(session_factory: Factory) -> None:
    """Requirement 10."""
    source = create_account(session_factory, funded=100)
    destination = create_account(session_factory)
    entries_before = ledger_entry_count(session_factory)

    rejections = [
        transfer(session_factory, source, destination, 101),  # insufficient funds
        transfer(session_factory, source, source, 10),  # same account
        transfer(session_factory, source, uuid.uuid4(), 10),  # missing destination
        transfer(session_factory, uuid.uuid4(), destination, 10),  # missing source
        transfer(session_factory, SYSTEM_FUNDING_ACCOUNT_ID, destination, 10),  # system source
    ]

    assert [r.error_code for r in rejections] == [
        "INSUFFICIENT_FUNDS",
        "SAME_ACCOUNT_TRANSFER",
        "ACCOUNT_NOT_FOUND",
        "ACCOUNT_NOT_FOUND",
        "SYSTEM_ACCOUNT_NOT_ALLOWED",
    ]
    assert ledger_entry_count(session_factory) == entries_before
    assert ledger_entry_count(session_factory, kind=TransferKind.TRANSFER) == 0


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE ledger_entries SET amount_minor = amount_minor * 2",
        "DELETE FROM ledger_entries",
        "UPDATE transfers SET amount_minor = 1",
        "DELETE FROM transfers",
    ],
)
def test_ledger_and_transfers_are_append_only(session_factory: Factory, statement: str) -> None:
    create_account(session_factory, funded=500)
    with session_factory() as session, pytest.raises(IntegrityError, match="append-only"):
        session.execute(text(statement))


def test_reconciliation_endpoint_reports_ok_and_detects_tampering(
    client: TestClient, session_factory: Factory
) -> None:
    a, b = api_create_account(client), api_create_account(client)
    api_fund_account(client, a["id"], 3_000)
    assert post_transfer(client, a["id"], b["id"], 1_000).status_code == 201

    healthy = client.get("/api/v1/reconciliation").json()
    assert healthy["ok"] is True
    assert healthy["ledger_total_minor"] == 0
    assert healthy["balance_total_minor"] == 0
    assert healthy["ledger_entries_checked"] == 4

    # Someone edits a balance behind the ledger's back (accounts are mutable by design).
    with session_factory() as session:
        session.execute(
            text("UPDATE accounts SET balance_minor = balance_minor + 1 WHERE id = :id"),
            {"id": b["id"]},
        )
        session.commit()

    broken = client.get("/api/v1/reconciliation").json()
    assert broken["ok"] is False
    assert broken["balance_mismatches"] == [
        {"account_id": b["id"], "balance_minor": 1_001, "ledger_balance_minor": 1_000}
    ]
    assert broken["balance_total_minor"] == 1
