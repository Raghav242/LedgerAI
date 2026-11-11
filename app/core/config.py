from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    """Application settings loaded from environment variables (and `.env` in development).

    Credentials have no defaults on purpose: the app refuses to start without them
    instead of silently falling back to a hardcoded password.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "LedgerFlow AI"
    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    enable_dev_endpoints: bool = False
    db_echo: bool = False

    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432
    postgres_db: str = "ledgerflow"
    postgres_test_db: str = "ledgerflow_test"
    postgres_user: str
    postgres_password: SecretStr

    # Connection pool. Requests beyond pool_size + max_overflow wait (pool_timeout) for a
    # free connection; that queueing is not a database lock conflict.
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=20, ge=0)
    db_pool_timeout_seconds: float = Field(default=30, gt=0)
    # Max wait for a row lock (or an in-flight request with the same Idempotency-Key)
    # before a transfer gives up with a retryable CONCURRENCY_CONFLICT.
    transfer_lock_timeout_ms: int = Field(default=3000, ge=1, le=60_000)

    @model_validator(mode="after")
    def _forbid_dev_endpoints_in_production(self) -> "Settings":
        if self.app_env == "production" and self.enable_dev_endpoints:
            raise ValueError("ENABLE_DEV_ENDPOINTS must be false when APP_ENV=production")
        return self

    def build_database_url(self, database: str) -> URL:
        # URL.create escapes special characters in the password; the URL's repr masks it.
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=database,
        )

    @property
    def database_url(self) -> URL:
        return self.build_database_url(self.postgres_db)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
