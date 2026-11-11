import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis
from fastapi import FastAPI

from app.api.error_handlers import register_exception_handlers
from app.api.middleware import ResponseHeadersMiddleware
from app.api.rate_limit import RateLimits, build_policies
from app.api.router import build_api_router
from app.api.routes import health
from app.core.config import Settings, get_settings
from app.core.database import create_db_engine, create_session_factory
from app.core.logging import configure_logging
from app.rate_limiting import (
    InMemoryFixedWindowLimiter,
    RateLimiter,
    RedisTokenBucketLimiter,
    create_redis_client,
)

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None, *, rate_limiter: RateLimiter | None = None
) -> FastAPI:
    """Application factory.

    Tests pass their own Settings (to point at the test database) and may inject a
    RateLimiter; otherwise the limiter is chosen by RATE_LIMIT_BACKEND.
    """
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    engine = create_db_engine(settings)

    redis_client: redis.Redis | None = None
    if rate_limiter is None:
        if settings.rate_limit_backend == "redis":
            redis_client = create_redis_client(settings)
            rate_limiter = RedisTokenBucketLimiter(
                redis_client, key_prefix=settings.rate_limit_key_prefix
            )
        else:
            rate_limiter = InMemoryFixedWindowLimiter()
    read_policy, write_policy = build_policies(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "Starting %s replica=%s env=%s database=%s rate_limiter=%s",
            settings.app_name,
            settings.replica_id,
            settings.app_env,
            engine.url.render_as_string(hide_password=True),
            type(rate_limiter).__name__,
        )
        if settings.enable_dev_endpoints:
            logger.warning("Development-only endpoints are ENABLED (POST /accounts/{id}/fund)")
        yield
        engine.dispose()
        if redis_client is not None:
            redis_client.close()
        logger.info("Database engine disposed")

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.session_factory = create_session_factory(engine)
    app.state.redis = redis_client
    app.state.rate_limits = RateLimits(limiter=rate_limiter, read=read_policy, write=write_policy)

    app.add_middleware(
        ResponseHeadersMiddleware,
        replica_id=settings.replica_id if settings.expose_replica_header else None,
    )
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(
        build_api_router(include_dev_endpoints=settings.enable_dev_endpoints), prefix="/api/v1"
    )
    return app
