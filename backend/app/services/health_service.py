"""Business logic for health checks (kept out of the route layer)."""

import logging
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.exceptions import DatabaseUnavailableError, ExtensionMissingError
from app.schemas.health import DatabaseHealthResponse

logger = logging.getLogger(__name__)

REQUIRED_EXTENSION = "vector"


async def check_database(engine: AsyncEngine) -> DatabaseHealthResponse:
    """Open a real connection, run trivial queries and confirm pgvector is installed.

    Raises DatabaseUnavailableError (503) if Postgres cannot be reached or queried,
    ExtensionMissingError (503) if pgvector is not enabled in this database.
    """
    started = time.perf_counter()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            database = (await conn.execute(text("SELECT current_database()"))).scalar_one()
            server_version = (await conn.execute(text("SHOW server_version"))).scalar_one()
            # Bound parameter, never string formatting -> no SQL injection surface.
            pgvector_version = (
                await conn.execute(
                    text("SELECT extversion FROM pg_extension WHERE extname = :name"),
                    {"name": REQUIRED_EXTENSION},
                )
            ).scalar_one_or_none()
    except Exception as exc:
        # A health check must turn *any* connection failure (refused, timeout, bad
        # credentials, driver error) into "unhealthy" rather than a 500.
        logger.warning("Database health check failed: %s: %s", type(exc).__name__, exc)
        raise DatabaseUnavailableError("Database is unreachable.") from exc

    if pgvector_version is None:
        raise ExtensionMissingError(
            f"PostgreSQL extension '{REQUIRED_EXTENSION}' is not installed in this database."
        )

    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    return DatabaseHealthResponse(
        status="ok",
        database=database,
        server_version=server_version,
        pgvector_version=pgvector_version,
        latency_ms=latency_ms,
    )
