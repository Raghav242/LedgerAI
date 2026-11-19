"""Read-only endpoints added for the frontend (Phase 5A): transfer lookup, ledger postings
and system summary. They must reflect committed state exactly and never move money."""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.models import SYSTEM_FUNDING_ACCOUNT_ID
from app.services.insights_service import InsightsService
from tests.integration.conftest import (
    create_account,
    fund_account,
    post_transfer,
)


def _two_funded_accounts(client: TestClient, amount: int = 10_000) -> tuple[dict, dict]:
    alice, bob = create_account(client, "Alice"), create_account(client, "Bob")
    fund_account(client, alice["id"], amount)
    return alice, bob


# --- GET /transfers/{id} ------------------------------------------------------------


def test_get_transfer_returns_the_created_transfer(client: TestClient) -> None:
    alice, bob = _two_funded_accounts(client)
    created = post_transfer(client, alice["id"], bob["id"], 2_550).json()

    response = client.get(f"/api/v1/transfers/{created['id']}")

    assert response.status_code == 200
    assert response.json() == created


def test_get_unknown_transfer_is_404_with_error_envelope(client: TestClient) -> None:
    missing = uuid.uuid4()
    response = client.get(f"/api/v1/transfers/{missing}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TRANSFER_NOT_FOUND"
    assert response.json()["error"]["details"] == {"transfer_id": str(missing)}


def test_get_transfer_rejects_malformed_id(client: TestClient) -> None:
    response = client.get("/api/v1/transfers/not-a-uuid")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# --- GET /transfers/{id}/ledger-entries ---------------------------------------------


def test_transfer_ledger_entries_are_a_balanced_debit_credit_pair(client: TestClient) -> None:
    alice, bob = _two_funded_accounts(client)
    transfer = post_transfer(client, alice["id"], bob["id"], 2_500).json()

    response = client.get(f"/api/v1/transfers/{transfer['id']}/ledger-entries")

    assert response.status_code == 200
    debit, credit = response.json()
    assert (debit["role"], debit["account_id"], debit["amount_minor"]) == (
        "debit",
        alice["id"],
        -2_500,
    )
    assert (credit["role"], credit["account_id"], credit["amount_minor"]) == (
        "credit",
        bob["id"],
        2_500,
    )
    assert {debit["transfer_id"], credit["transfer_id"]} == {transfer["id"]}
    assert debit["amount_minor"] + credit["amount_minor"] == 0


def test_funding_ledger_entries_debit_the_system_account(client: TestClient) -> None:
    alice = create_account(client, "Alice")
    fund_account(client, alice["id"], 700)
    funding = client.get(
        "/api/v1/transfers", params={"account_id": alice["id"], "kind": "dev_funding"}
    ).json()["items"][0]

    debit, credit = client.get(f"/api/v1/transfers/{funding['id']}/ledger-entries").json()

    assert debit["account_id"] == str(SYSTEM_FUNDING_ACCOUNT_ID)
    assert debit["amount_minor"] == -700
    assert credit["account_id"] == alice["id"]


def test_ledger_entries_of_unknown_transfer_is_404(client: TestClient) -> None:
    response = client.get(f"/api/v1/transfers/{uuid.uuid4()}/ledger-entries")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TRANSFER_NOT_FOUND"


# --- GET /ledger/entries --------------------------------------------------------------


def test_ledger_entries_are_newest_first_with_pairs_adjacent(client: TestClient) -> None:
    alice, bob = _two_funded_accounts(client)
    first = post_transfer(client, alice["id"], bob["id"], 100).json()
    second = post_transfer(client, bob["id"], alice["id"], 40).json()

    items = client.get("/api/v1/ledger/entries", params={"limit": 100}).json()["items"]

    # funding (2 entries) + two transfers (2 each)
    assert len(items) == 6
    assert [e["transfer_id"] for e in items[:4]] == [second["id"]] * 2 + [first["id"]] * 2
    assert [e["role"] for e in items] == ["debit", "credit"] * 3
    assert sum(e["amount_minor"] for e in items) == 0


def test_ledger_entries_filter_by_account_and_paginate(client: TestClient) -> None:
    alice, bob = _two_funded_accounts(client)
    carol = create_account(client, "Carol")
    for _ in range(3):
        post_transfer(client, alice["id"], bob["id"], 10)
    post_transfer(client, alice["id"], carol["id"], 10)

    bob_entries = client.get(
        "/api/v1/ledger/entries", params={"account_id": bob["id"], "limit": 100}
    ).json()["items"]
    assert len(bob_entries) == 3
    assert {e["account_id"] for e in bob_entries} == {bob["id"]}
    assert all(e["role"] == "credit" and e["amount_minor"] == 10 for e in bob_entries)

    page = client.get(
        "/api/v1/ledger/entries", params={"account_id": alice["id"], "limit": 2, "offset": 2}
    ).json()
    assert (page["limit"], page["offset"]) == (2, 2)
    # Alice: 4 debits + 1 funding credit = 5 entries; offset 2 limit 2 -> 2 items.
    assert len(page["items"]) == 2


def test_ledger_entries_limit_is_bounded(client: TestClient) -> None:
    assert client.get("/api/v1/ledger/entries", params={"limit": 101}).status_code == 422


# --- GET /stats/summary ---------------------------------------------------------------


def test_summary_of_empty_system(client: TestClient) -> None:
    body = client.get("/api/v1/stats/summary").json()

    assert body["merchant_account_count"] == 0
    assert body["merchant_balance_total_minor"] == 0
    assert body["ledger_entry_count"] == 0
    assert {k["kind"]: (k["count"], k["volume_minor"]) for k in body["transfers_by_kind"]} == {
        "transfer": (0, 0),
        "dev_funding": (0, 0),
        "legacy_opening_balance": (0, 0),
    }
    assert len(body["daily_transfer_activity"]) == 14
    assert all(d["count"] == 0 for d in body["daily_transfer_activity"])


def test_summary_counts_accounts_transfers_and_volume(client: TestClient) -> None:
    alice, bob = _two_funded_accounts(client, 10_000)
    post_transfer(client, alice["id"], bob["id"], 2_500)
    post_transfer(client, bob["id"], alice["id"], 500)
    # Rejected transfers are not transfers: they must not count.
    assert post_transfer(client, alice["id"], bob["id"], 1_000_000).status_code == 409

    body = client.get("/api/v1/stats/summary", params={"activity_days": 3}).json()

    assert body["merchant_account_count"] == 2
    assert body["merchant_balance_total_minor"] == 10_000  # transfers conserve money
    assert body["ledger_entry_count"] == 6
    kinds = {k["kind"]: (k["count"], k["volume_minor"]) for k in body["transfers_by_kind"]}
    assert kinds["transfer"] == (2, 3_000)
    assert kinds["dev_funding"] == (1, 10_000)

    activity = body["daily_transfer_activity"]
    assert len(activity) == 3
    assert activity[-1]["date"] == datetime.now(UTC).date().isoformat()
    assert (activity[-1]["count"], activity[-1]["volume_minor"]) == (2, 3_000)
    assert sum(d["count"] for d in activity) == 2


def test_summary_activity_days_is_bounded(client: TestClient) -> None:
    assert client.get("/api/v1/stats/summary", params={"activity_days": 0}).status_code == 422
    assert client.get("/api/v1/stats/summary", params={"activity_days": 91}).status_code == 422


def test_summary_activity_window_excludes_older_days(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    alice, bob = _two_funded_accounts(client)
    post_transfer(client, alice["id"], bob["id"], 100)

    # Viewed from 10 days in the future with a 3-day window, today's transfer is outside.
    with session_factory() as session:
        future = datetime.now(UTC) + timedelta(days=10)
        summary = InsightsService(session).summary(activity_days=3, now=future)

    assert [d.day for d in summary.daily_transfer_activity][-1] == future.date()
    assert sum(d.count for d in summary.daily_transfer_activity) == 0
    # Totals are not windowed.
    assert next(k for k in summary.transfers_by_kind if k.kind == "transfer").count == 1


def test_read_endpoints_do_not_move_money(client: TestClient) -> None:
    alice, bob = _two_funded_accounts(client)
    transfer = post_transfer(client, alice["id"], bob["id"], 100).json()
    before = client.get("/api/v1/reconciliation").json()

    client.get(f"/api/v1/transfers/{transfer['id']}")
    client.get(f"/api/v1/transfers/{transfer['id']}/ledger-entries")
    client.get("/api/v1/ledger/entries")
    client.get("/api/v1/stats/summary")

    after = client.get("/api/v1/reconciliation").json()
    assert after == before
    assert after["ok"] is True


# --- GET /accounts?order= -------------------------------------------------------------


def test_accounts_default_to_oldest_first_and_can_list_newest_first(client: TestClient) -> None:
    names = [create_account(client, f"Acct {i}")["name"] for i in range(3)]

    default = client.get("/api/v1/accounts").json()["items"]
    newest = client.get("/api/v1/accounts", params={"order": "desc"}).json()["items"]
    newest_page = client.get("/api/v1/accounts", params={"order": "desc", "limit": 1}).json()

    assert [a["name"] for a in default] == names
    assert [a["name"] for a in newest] == names[::-1]
    assert [a["name"] for a in newest_page["items"]] == [names[-1]]


def test_accounts_order_is_validated(client: TestClient) -> None:
    response = client.get("/api/v1/accounts", params={"order": "sideways"})
    assert response.status_code == 422
