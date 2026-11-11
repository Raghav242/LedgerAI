"""Atomicity: a failure at any step of a transfer must leave no trace at all.

Not in balances, not in transfers, not in ledger_entries, and not in idempotency_records
(so the same Idempotency-Key can be retried). Failures are injected *after* balances
were already modified in the session, the moment a partial write would be possible.
"""

import uuid

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    SYSTEM_FUNDING_ACCOUNT_ID,
    Account,
    LedgerEntry,
    LedgerEntryRole,
    Transfer,
)
from app.repositories.idempotency_repository import IdempotencyRepository
from app.repositories.ledger_repository import LedgerRepository
from app.repositories.transfer_repository import TransferRepository
from app.services import ledger_posting
from tests.integration.support import (
    balance,
    create_account,
    idempotency_record,
    ledger_entry_count,
    merchant_transfer_count,
    transfer,
)

Factory = sessionmaker[Session]


def _assert_untouched(
    factory: Factory, source: uuid.UUID, destination: uuid.UUID, key: str
) -> None:
    assert balance(factory, source) == 1_000
    assert balance(factory, destination) == 0
    assert merchant_transfer_count(factory) == 0
    assert ledger_entry_count(factory) == 2  # only the funding posting
    assert idempotency_record(factory, key) is None


def _boom(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("simulated failure")


@pytest.mark.parametrize(
    ("target", "attribute"),
    [
        (TransferRepository, "add"),  # writing the transfer row
        (LedgerRepository, "add_postings"),  # writing the ledger entries   (requirement 7)
        (IdempotencyRepository, "complete"),  # storing the outcome         (requirement 8)
    ],
    ids=["transfer-insert", "ledger-insert", "idempotency-complete"],
)
def test_failure_at_any_step_rolls_back_everything(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch, target: type, attribute: str
) -> None:
    source = create_account(session_factory, funded=1_000)
    destination = create_account(session_factory)
    key = str(uuid.uuid4())

    with monkeypatch.context() as patch:
        patch.setattr(target, attribute, _boom)
        with pytest.raises(RuntimeError, match="simulated failure"):
            transfer(session_factory, source, destination, 400, key=key)

    _assert_untouched(session_factory, source, destination, key)

    # The claim was rolled back too, so the very same key now executes normally, once.
    result = transfer(session_factory, source, destination, 400, key=key)
    assert result.succeeded and not result.replayed
    assert balance(session_factory, source) == 600
    assert merchant_transfer_count(session_factory) == 1


def _unbalanced_postings(self: LedgerRepository, transfer_row: Transfer) -> list[LedgerEntry]:
    """Debit -amount, credit amount+1: each row is individually valid, the pair is not."""
    entries = [
        LedgerEntry(
            transfer_id=transfer_row.id,
            account_id=transfer_row.source_account_id,
            role=LedgerEntryRole.DEBIT,
            amount_minor=-transfer_row.amount_minor,
            currency=transfer_row.currency,
        ),
        LedgerEntry(
            transfer_id=transfer_row.id,
            account_id=transfer_row.destination_account_id,
            role=LedgerEntryRole.CREDIT,
            amount_minor=transfer_row.amount_minor + 1,
            currency=transfer_row.currency,
        ),
    ]
    self._session.add_all(entries)
    self._session.flush()
    return entries


def _duplicate_credit(self: LedgerRepository, transfer_row: Transfer) -> list[LedgerEntry]:
    entries = REAL_ADD_POSTINGS(self, transfer_row)
    self._session.add(
        LedgerEntry(
            transfer_id=transfer_row.id,
            account_id=transfer_row.destination_account_id,
            role=LedgerEntryRole.CREDIT,
            amount_minor=1,
            currency=transfer_row.currency,
        )
    )
    self._session.flush()
    return entries


REAL_ADD_POSTINGS = LedgerRepository.add_postings


@pytest.mark.parametrize(
    ("bad_postings", "expected"),
    [
        # Deferred constraint trigger: checked at COMMIT once both entries exist.
        (_unbalanced_postings, "ledger invariant violated"),
        # Unique (transfer_id, role): a second credit posting for the same transfer.
        (_duplicate_credit, "uq_ledger_entries_transfer_id_role"),
    ],
    ids=["entries-do-not-sum-to-zero", "duplicate-posting"],
)
def test_invalid_ledger_postings_are_rejected_by_database(
    session_factory: Factory,
    monkeypatch: pytest.MonkeyPatch,
    bad_postings: object,
    expected: str,
) -> None:
    source = create_account(session_factory, funded=1_000)
    destination = create_account(session_factory)
    key = str(uuid.uuid4())

    monkeypatch.setattr(LedgerRepository, "add_postings", bad_postings)
    with pytest.raises(IntegrityError, match=expected):
        transfer(session_factory, source, destination, 400, key=key)

    _assert_untouched(session_factory, source, destination, key)


def test_transfer_without_ledger_entries_is_rejected_at_commit(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = create_account(session_factory, funded=1_000)
    destination = create_account(session_factory)
    key = str(uuid.uuid4())

    with monkeypatch.context() as patch:
        patch.setattr(LedgerRepository, "add_postings", lambda self, transfer_row: [])
        with pytest.raises(IntegrityError, match="ledger invariant violated"):
            transfer(session_factory, source, destination, 400, key=key)

    _assert_untouched(session_factory, source, destination, key)


def test_database_check_constraint_rolls_back_whole_transfer(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defense in depth: even if the funds check were buggy, CHECK (balance >= 0) rejects
    the overdraft and the credit, transfer row, entries and claim all roll back."""
    source = create_account(session_factory, funded=1_000)
    destination = create_account(session_factory)
    key = str(uuid.uuid4())
    monkeypatch.setattr(ledger_posting, "validate_transfer_against_accounts", lambda *a, **k: None)

    with pytest.raises(IntegrityError, match="ck_accounts_balance_non_negative"):
        transfer(session_factory, source, destination, 5_000, key=key)

    _assert_untouched(session_factory, source, destination, key)


def test_session_is_usable_after_a_rolled_back_transfer(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = create_account(session_factory, funded=1_000)
    destination = create_account(session_factory)
    from app.services.transfer_service import TransferCommand, TransferService

    with session_factory() as session:
        service = TransferService(session, lock_timeout_ms=5_000)
        command = TransferCommand(source, destination, 300, "USD")
        with monkeypatch.context() as patch:
            patch.setattr(LedgerRepository, "add_postings", _boom)
            with pytest.raises(RuntimeError):
                service.create_transfer(scope="s", idempotency_key="k1", command=command)
        result = service.create_transfer(scope="s", idempotency_key="k2", command=command)
        assert result.succeeded

    assert balance(session_factory, source) == 700
    assert balance(session_factory, destination) == 300


def test_negative_merchant_balance_is_rejected_by_database(session_factory: Factory) -> None:
    account_id = create_account(session_factory)
    with session_factory() as session, pytest.raises(IntegrityError):
        session.execute(update(Account).where(Account.id == account_id).values(balance_minor=-1))
        session.commit()


def test_system_account_may_hold_negative_balance(session_factory: Factory) -> None:
    create_account(session_factory, funded=2_500)
    assert balance(session_factory, SYSTEM_FUNDING_ACCOUNT_ID) == -2_500


@pytest.mark.parametrize(
    ("amount", "same_account", "constraint"),
    [
        (0, False, "ck_transfers_amount_positive"),
        (1, True, "ck_transfers_distinct_accounts"),
    ],
)
def test_transfer_table_constraints(
    session_factory: Factory, amount: int, same_account: bool, constraint: str
) -> None:
    source = create_account(session_factory)
    destination = source if same_account else create_account(session_factory)
    with session_factory() as session, pytest.raises(IntegrityError, match=constraint):
        session.execute(
            text(
                "INSERT INTO transfers (id, source_account_id, destination_account_id, "
                "amount_minor, currency, status) VALUES (:id, :s, :d, :amount, 'USD', 'completed')"
            ),
            {"id": uuid.uuid4(), "s": source, "d": destination, "amount": amount},
        )
