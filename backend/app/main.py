"""FastAPI application factory and lifespan (startup/shutdown) management."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.db.session import create_db_engine, create_session_factory

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    engine = create_db_engine(settings)
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    # Note: we do not fail startup if the DB is down. The engine connects lazily;
    # /api/v1/health/db reports readiness. This keeps the API debuggable.
    logger.info(
        "Started %s v%s (%s) -> db %s:%s/%s",
        settings.app_name,
        __version__,
        settings.environment,
        settings.postgres_host,
        settings.postgres_port,
        settings.postgres_db,
    )
    try:
        yield
    finally:
        await engine.dispose()
        logger.info("Database engine disposed; shutdown complete.")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description="AI-powered research intelligence and literature review platform.",
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_v1_prefix)
    return app


app = create_app()
