import uuid

from fastapi.testclient import TestClient

from tests.integration.conftest import (
    create_account,
    fund_account,
    get_balance,
    post_transfer,
)


def _count_transfers(client: TestClient) -> int:
    return len(client.get("/api/v1/transfers", params={"limit": 100}).json()["items"])


def test_successful_transfer_updates_both_balances_and_persists_record(
    client: TestClient,
) -> None:
    alice = create_account(client, "Alice")
    bob = create_account(client, "Bob")
    fund_account(client, alice["id"], 10_000)

    response = post_transfer(client, alice["id"], bob["id"], 2_550)

    assert response.status_code == 201, response.text
    transfer = response.json()
    assert transfer["source_account_id"] == alice["id"]
    assert transfer["destination_account_id"] == bob["id"]
    assert transfer["amount_minor"] == 2_550
    assert transfer["currency"] == "USD"
    assert transfer["status"] == "completed"
    assert get_balance(client, alice["id"]) == 7_450
    assert get_balance(client, bob["id"]) == 2_550

    history = client.get("/api/v1/transfers").json()["items"]
    assert [t["id"] for t in history] == [transfer["id"]]


def test_transferring_entire_balance_is_allowed(client: TestClient) -> None:
    a, b = create_account(client), create_account(client)
    fund_account(client, a["id"], 500)
    assert post_transfer(client, a["id"], b["id"], 500).status_code == 201
    assert get_balance(client, a["id"]) == 0


def test_total_balance_is_conserved_across_many_transfers(client: TestClient) -> None:
    accounts = [create_account(client, f"A{i}")["id"] for i in range(4)]
    for account_id in accounts:
        fund_account(client, account_id, 10_000)
    total_before = sum(get_balance(client, a) for a in accounts)

    moves = [(0, 1, 3_000), (1, 2, 7_000), (2, 3, 1), (3, 0, 9_999), (0, 2, 50_000)]
    for src, dst, amount in moves:
        post_transfer(client, accounts[src], accounts[dst], amount)  # last one fails (funds)

    total_after = sum(get_balance(client, a) for a in accounts)
    assert total_after == total_before == 40_000
    assert all(get_balance(client, a) >= 0 for a in accounts)
    assert _count_transfers(client) == 4


def test_insufficient_funds_returns_409_and_changes_nothing(client: TestClient) -> None:
    a, b = create_account(client), create_account(client)
    fund_account(client, a["id"], 1_000)

    response = post_transfer(client, a["id"], b["id"], 1_001)

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "INSUFFICIENT_FUNDS"
    assert error["details"]["balance_minor"] == 1_000
    assert get_balance(client, a["id"]) == 1_000
    assert get_balance(client, b["id"]) == 0
    assert _count_transfers(client) == 0


def test_zero_or_negative_amount_returns_422(client: TestClient) -> None:
    a, b = create_account(client), create_account(client)
    fund_account(client, a["id"], 1_000)
    for amount in (0, -1, -1_000):
        response = post_transfer(client, a["id"], b["id"], amount)
        assert response.status_code == 422, amount
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _count_transfers(client) == 0


def test_fractional_amount_returns_422(client: TestClient) -> None:
    a, b = create_account(client), create_account(client)
    fund_account(client, a["id"], 1_000)
    response = client.post(
        "/api/v1/transfers",
        json={
            "source_account_id": a["id"],
            "destination_account_id": b["id"],
            "amount_minor": 10.5,
            "currency": "USD",
        },
    )
    assert response.status_code == 422


def test_same_source_and_destination_returns_422(client: TestClient) -> None:
    a = create_account(client)
    fund_account(client, a["id"], 1_000)

    response = post_transfer(client, a["id"], a["id"], 100)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "SAME_ACCOUNT_TRANSFER"
    assert get_balance(client, a["id"]) == 1_000


def test_missing_source_account_returns_404(client: TestClient) -> None:
    b = create_account(client)
    response = post_transfer(client, str(uuid.uuid4()), b["id"], 100)
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "ACCOUNT_NOT_FOUND"
    assert error["details"]["role"] == "source"


def test_missing_destination_account_returns_404_and_does_not_debit(client: TestClient) -> None:
    a = create_account(client)
    fund_account(client, a["id"], 1_000)

    response = post_transfer(client, a["id"], str(uuid.uuid4()), 100)

    assert response.status_code == 404
    assert response.json()["error"]["details"]["role"] == "destination"
    assert get_balance(client, a["id"]) == 1_000


def test_unsupported_transfer_currency_returns_422(client: TestClient) -> None:
    a, b = create_account(client), create_account(client)
    response = client.post(
        "/api/v1/transfers",
        json={
            "source_account_id": a["id"],
            "destination_account_id": b["id"],
            "amount_minor": 100,
            "currency": "EUR",
        },
    )
    assert response.status_code == 422


def test_missing_fields_return_422_with_field_locations(client: TestClient) -> None:
    response = client.post("/api/v1/transfers", json={"amount_minor": 100})
    assert response.status_code == 422
    locations = {tuple(d["loc"]) for d in response.json()["error"]["details"]}
    assert ("body", "source_account_id") in locations
    assert ("body", "destination_account_id") in locations
    assert ("body", "currency") in locations


def test_transfer_history_is_newest_first_and_filterable(client: TestClient) -> None:
    a, b, c = (create_account(client)["id"] for _ in range(3))
    fund_account(client, a, 10_000)
    first = post_transfer(client, a, b, 100).json()
    second = post_transfer(client, a, c, 200).json()

    all_ids = [t["id"] for t in client.get("/api/v1/transfers").json()["items"]]
    assert all_ids == [second["id"], first["id"]]

    for_b = client.get("/api/v1/transfers", params={"account_id": b}).json()["items"]
    assert [t["id"] for t in for_b] == [first["id"]]
