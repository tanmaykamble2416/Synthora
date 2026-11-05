"""Day 6: document structure of a paper (sections, chunks, references). Owner-only, read-only."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, DbSession, SettingsDep, StorageDep
from app.schemas.common import Page
from app.schemas.error import ErrorResponse
from app.schemas.structure import PaperChunkRead, PaperReferenceRead, PaperSectionRead
from app.services.extraction_service import ExtractionService

router = APIRouter(
    prefix="/papers/{paper_id}",
    tags=["structure"],
    responses={
        401: {"model": ErrorResponse, "description": "Not authenticated"},
        404: {"model": ErrorResponse, "description": "Paper not found, or owned by another user"},
    },
)

Offset = Annotated[int, Query(ge=0, le=100_000)]


@router.get(
    "/sections",
    response_model=Page[PaperSectionRead],
    summary="List the sections of one of my papers, in reading order",
    description=(
        "Sections come from GROBID (`structure_source` = `grobid`). Empty when the paper was "
        "processed with the page fallback or not processed yet."
    ),
)
async def list_paper_sections(
    paper_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    storage: StorageDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Offset = 0,
) -> Page[PaperSectionRead]:
    return await ExtractionService(session, storage, settings).list_sections(
        user.id, paper_id, limit=limit, offset=offset
    )


@router.get(
    "/chunks",
    response_model=Page[PaperChunkRead],
    summary="List the retrieval chunks of one of my papers, in reading order",
    description=(
        "Each chunk has an exact 1-based page range for citations. `token_count` is measured by "
        "`token_count_method`: `word_proxy` means whitespace-separated words, not model tokens."
    ),
)
async def list_paper_chunks(
    paper_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    storage: StorageDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Offset = 0,
) -> Page[PaperChunkRead]:
    return await ExtractionService(session, storage, settings).list_chunks(
        user.id, paper_id, limit=limit, offset=offset
    )


@router.get(
    "/references",
    response_model=Page[PaperReferenceRead],
    summary="List the bibliography of one of my papers, as parsed by GROBID",
    description="Fields GROBID could not determine are null. Empty when GROBID was not used.",
)
async def list_paper_references(
    paper_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    storage: StorageDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Offset = 0,
) -> Page[PaperReferenceRead]:
    return await ExtractionService(session, storage, settings).list_references(
        user.id, paper_id, limit=limit, offset=offset
    )
