"""Health endpoints. Routes only translate HTTP <-> service calls."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncEngine

from app import __version__
from app.api.deps import GrobidDep
from app.core.config import Settings, get_settings
from app.db.session import get_engine
from app.schemas.error import ErrorResponse
from app.schemas.health import DatabaseHealthResponse, GrobidHealthResponse, LivenessResponse
from app.services.health_service import check_database, check_grobid

router = APIRouter(prefix="/health", tags=["health"])


@router.get("", response_model=LivenessResponse, summary="Liveness: is the API process up?")
async def liveness(settings: Annotated[Settings, Depends(get_settings)]) -> LivenessResponse:
    # Deliberately does NOT touch the database: a DB outage should not make the
    # orchestrator think the API process itself is dead.
    return LivenessResponse(
        status="ok",
        app_name=settings.app_name,
        version=__version__,
        environment=settings.environment,
    )


@router.get(
    "/db",
    response_model=DatabaseHealthResponse,
    summary="Readiness: can the API reach PostgreSQL with pgvector enabled?",
    responses={503: {"model": ErrorResponse, "description": "Database or pgvector unavailable"}},
)
async def database_health(
    engine: Annotated[AsyncEngine, Depends(get_engine)],
) -> DatabaseHealthResponse:
    return await check_database(engine)


@router.get(
    "/grobid",
    response_model=GrobidHealthResponse,
    summary="Is the optional GROBID service reachable?",
    responses={503: {"model": ErrorResponse, "description": "GROBID enabled but unreachable"}},
)
async def grobid_health(grobid: GrobidDep) -> GrobidHealthResponse:
    return await check_grobid(grobid)
