import uuid

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from tests.integration.conftest import create_account, fund_account


def test_create_account_returns_201_with_zero_balance(client: TestClient) -> None:
    response = client.post("/api/v1/accounts", json={"name": "Acme Store"})

    assert response.status_code == 201
    body = response.json()
    assert uuid.UUID(body["id"])
    assert body["name"] == "Acme Store"
    assert body["currency"] == "USD"
    assert body["balance_minor"] == 0
    assert body["created_at"]


def test_create_account_rejects_client_supplied_balance(client: TestClient) -> None:
    response = client.post("/api/v1/accounts", json={"name": "Sneaky", "balance_minor": 10**6})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_create_account_rejects_unsupported_currency(client: TestClient) -> None:
    response = client.post("/api/v1/accounts", json={"name": "Euro Shop", "currency": "EUR"})
    assert response.status_code == 422


def test_get_account_by_id(client: TestClient) -> None:
    created = create_account(client, "Lookup")
    response = client.get(f"/api/v1/accounts/{created['id']}")
    assert response.status_code == 200
    assert response.json() == created


def test_get_missing_account_returns_404(client: TestClient) -> None:
    response = client.get(f"/api/v1/accounts/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ACCOUNT_NOT_FOUND"


def test_get_account_with_malformed_id_returns_422(client: TestClient) -> None:
    response = client.get("/api/v1/accounts/not-a-uuid")
    assert response.status_code == 422


def test_list_accounts_with_pagination(client: TestClient) -> None:
    ids = [create_account(client, f"Merchant {i}")["id"] for i in range(3)]

    response = client.get("/api/v1/accounts", params={"limit": 2, "offset": 0})
    assert response.status_code == 200
    page = response.json()
    assert page["limit"] == 2
    assert [a["id"] for a in page["items"]] == ids[:2]

    rest = client.get("/api/v1/accounts", params={"limit": 2, "offset": 2}).json()
    assert [a["id"] for a in rest["items"]] == ids[2:]


def test_list_accounts_rejects_out_of_range_limit(client: TestClient) -> None:
    assert client.get("/api/v1/accounts", params={"limit": 0}).status_code == 422
    assert client.get("/api/v1/accounts", params={"limit": 101}).status_code == 422


def test_fund_account_increases_balance(client: TestClient) -> None:
    account = create_account(client)
    fund_account(client, account["id"], 5_000)
    funded = fund_account(client, account["id"], 2_500)
    assert funded["balance_minor"] == 7_500

    balance = client.get(f"/api/v1/accounts/{account['id']}/balance").json()
    assert balance == {"account_id": account["id"], "currency": "USD", "balance_minor": 7_500}


def test_fund_account_rejects_non_positive_and_fractional_amounts(client: TestClient) -> None:
    account = create_account(client)
    for amount in (0, -100, 10.5, "100"):
        response = client.post(
            f"/api/v1/accounts/{account['id']}/fund", json={"amount_minor": amount}
        )
        assert response.status_code == 422, amount
    assert client.get(f"/api/v1/accounts/{account['id']}").json()["balance_minor"] == 0


def test_fund_missing_account_returns_404(client: TestClient) -> None:
    response = client.post(f"/api/v1/accounts/{uuid.uuid4()}/fund", json={"amount_minor": 100})
    assert response.status_code == 404


def test_fund_endpoint_absent_when_dev_endpoints_disabled(test_settings: Settings) -> None:
    app = create_app(test_settings.model_copy(update={"enable_dev_endpoints": False}))
    with TestClient(app) as client:
        account = create_account(client)
        response = client.post(f"/api/v1/accounts/{account['id']}/fund", json={"amount_minor": 100})
        assert response.status_code == 404
        paths = client.get("/openapi.json").json()["paths"]
        assert "/api/v1/accounts/{account_id}/fund" not in paths
