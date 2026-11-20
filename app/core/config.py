import socket
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

    # Connection pool, PER PROCESS (each API replica has its own). Requests beyond
    # pool_size + max_overflow wait up to pool_timeout for a free connection; that queueing
    # is not a database lock conflict. Across N replicas the database may see up to
    # N * (pool_size + max_overflow) connections: keep that below max_connections.
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=20, ge=0)
    db_pool_timeout_seconds: float = Field(default=30, gt=0)
    # Replace pooled connections older than this (seconds); -1 disables recycling.
    db_pool_recycle_seconds: int = Field(default=1800, ge=-1)
    # Max wait for a row lock (or an in-flight request with the same Idempotency-Key)
    # before a transfer gives up with a retryable CONCURRENCY_CONFLICT.
    transfer_lock_timeout_ms: int = Field(default=3000, ge=1, le=60_000)

    # --- Horizontal scaling ------------------------------------------------------------
    # Identifies the replica in logs, /health/live and (when enabled) the X-Served-By header.
    replica_id: str = Field(default_factory=socket.gethostname)
    # Development aid: add X-Served-By to every response. Leaks topology; keep off in production.
    expose_replica_header: bool = False

    # --- Rate limiting -----------------------------------------------------------------
    # "redis" is the distributed limiter (shared by all replicas). "memory" is a
    # per-process fixed window used for comparison and tests; it is refused in production.
    rate_limit_backend: Literal["redis", "memory"] = "redis"
    rate_limit_key_prefix: str = "ledgerflow:rl"
    # Token bucket: up to `capacity` requests in a burst, refilled continuously at
    # `refill_tokens` per `refill_interval_ms`.
    rate_limit_write_capacity: int = Field(default=20, ge=1)
    rate_limit_write_refill_tokens: int = Field(default=10, ge=1)
    rate_limit_write_refill_interval_ms: int = Field(default=1000, ge=1)
    rate_limit_read_capacity: int = Field(default=100, ge=1)
    rate_limit_read_refill_tokens: int = Field(default=50, ge=1)
    rate_limit_read_refill_interval_ms: int = Field(default=1000, ge=1)

    redis_url: str = "redis://127.0.0.1:6379/0"
    # Strict timeouts: a slow or dead Redis must not hold API threads hostage.
    redis_connect_timeout_ms: int = Field(default=250, ge=1)
    redis_socket_timeout_ms: int = Field(default=250, ge=1)
    # Per process. Uvicorn runs sync endpoints on a 40-thread pool, so 50 never queues.
    redis_max_connections: int = Field(default=50, ge=1)

    # --- Events: transactional outbox + RabbitMQ ----------------------------------------
    # Only the outbox publisher and event consumer talk to RabbitMQ; the API never does.
    rabbitmq_host: str = "127.0.0.1"
    rabbitmq_port: int = 5672
    rabbitmq_user: str = "ledgerflow"
    rabbitmq_password: SecretStr | None = None  # required by the publisher and consumer
    rabbitmq_vhost: str = "/"
    rabbitmq_heartbeat_seconds: int = Field(default=30, ge=0)
    rabbitmq_connect_timeout_seconds: float = Field(default=5, gt=0)
    # Prefix for exchange/queue names; tests use a unique prefix for isolation.
    events_topology_prefix: str = "ledgerflow"

    outbox_batch_size: int = Field(default=100, ge=1, le=1000)
    # A claimed batch is "processing" for at most this long; a crashed publisher's claim
    # then expires and another publisher re-claims (and re-publishes) the events.
    outbox_lease_seconds: int = Field(default=30, ge=1)
    outbox_poll_interval_ms: int = Field(default=500, ge=10)
    outbox_retry_base_ms: int = Field(default=500, ge=1)
    outbox_retry_max_ms: int = Field(default=60_000, ge=1)

    consumer_name: str = "ledger-audit"
    consumer_prefetch: int = Field(default=10, ge=1, le=1000)
    # Delays of the retry tiers for transient failures; one tier per retry attempt, so
    # the number of tiers is the retry limit. After that the event is dead-lettered.
    consumer_retry_delays_ms: list[int] = Field(default=[1_000, 5_000, 25_000], min_length=1)
    # A message redelivered more often than this (e.g. it crashes the consumer every
    # time) is dead-lettered by RabbitMQ itself (quorum queue delivery limit).
    consumer_delivery_limit: int = Field(default=5, ge=1)
    # DEVELOPMENT-ONLY fault injection for the worker-crash demo: hard-exit the consumer
    # while processing this event id, before or after its database commit. Refused in
    # production.
    consumer_crash_before_commit_event: str | None = None
    consumer_crash_after_commit_event: str | None = None

    # --- Experiment Lab (Phase 5B) -----------------------------------------------------
    # The lab is part of the development-only surface: its routes exist only when
    # ENABLE_DEV_ENDPOINTS=true (it mints simulated money for its own isolated accounts).
    # Where the runner sends the *measured* experiment traffic. In the Compose stack this is
    # NGINX on the internal network, so lab requests are load balanced like any client's.
    # Operator configuration only: never derived from a request.
    lab_target_base_url: str = Field(
        default="http://127.0.0.1:8000", pattern=r"^https?://[A-Za-z0-9.\-]+(:\d+)?$"
    )
    # Signs lab traffic so replicas give it its own rate-limit and idempotency scope. When
    # unset, a key is derived from POSTGRES_PASSWORD, which every replica already shares.
    lab_traffic_secret: SecretStr | None = None

    @model_validator(mode="after")
    def _forbid_unsafe_production_settings(self) -> "Settings":
        if self.app_env == "production" and self.enable_dev_endpoints:
            raise ValueError("ENABLE_DEV_ENDPOINTS must be false when APP_ENV=production")
        if self.app_env == "production" and (
            self.consumer_crash_before_commit_event or self.consumer_crash_after_commit_event
        ):
            raise ValueError("Consumer fault injection is not allowed when APP_ENV=production")
        if self.app_env == "production" and self.rate_limit_backend == "memory":
            raise ValueError(
                "RATE_LIMIT_BACKEND=memory is per-process and not a real limit behind a "
                "load balancer; use redis when APP_ENV=production"
            )
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
