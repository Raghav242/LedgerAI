import pytest
from pydantic import ValidationError

from app.core.config import Settings


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"postgres_user": "u", "postgres_password": "s3cr3t"}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def test_dev_endpoints_are_disabled_by_default() -> None:
    assert make_settings().enable_dev_endpoints is False


def test_production_refuses_dev_endpoints() -> None:
    with pytest.raises(ValidationError):
        make_settings(app_env="production", enable_dev_endpoints=True)


def test_password_is_not_leaked_in_repr() -> None:
    settings = make_settings()
    assert "s3cr3t" not in repr(settings)
    assert "s3cr3t" not in repr(settings.database_url)


def test_database_url_escapes_special_characters() -> None:
    url = make_settings(postgres_password="p@ss:w/rd").database_url
    assert url.password == "p@ss:w/rd"
    assert url.drivername == "postgresql+psycopg"
