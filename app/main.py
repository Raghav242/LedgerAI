import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.error_handlers import register_exception_handlers
from app.api.router import build_api_router
from app.api.routes import health
from app.core.config import Settings, get_settings
from app.core.database import create_db_engine, create_session_factory
from app.core.logging import configure_logging

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory. Tests pass their own Settings to point at the test database."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    engine = create_db_engine(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "Starting %s env=%s database=%s",
            settings.app_name,
            settings.app_env,
            engine.url.render_as_string(hide_password=True),
        )
        if settings.enable_dev_endpoints:
            logger.warning("Development-only endpoints are ENABLED (POST /accounts/{id}/fund)")
        yield
        engine.dispose()
        logger.info("Database engine disposed")

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.session_factory = create_session_factory(engine)

    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(
        build_api_router(include_dev_endpoints=settings.enable_dev_endpoints), prefix="/api/v1"
    )
    return app
