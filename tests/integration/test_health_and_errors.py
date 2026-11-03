from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient


def test_health_reports_database_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_unknown_route_uses_error_envelope(client: TestClient) -> None:
    response = client.get("/api/v1/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_malformed_json_returns_validation_error(client: TestClient) -> None:
    response = client.post(
        "/api/v1/accounts", content="{not json", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_migrations_match_orm_models(alembic_config: Config, engine: object) -> None:
    # Fails if someone changes a model without writing a migration.
    command.check(alembic_config)
