"""Day 5: PDF text extraction and access to page-level text. All routes are owner-only."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.api.deps import CurrentUser, DbSession, SettingsDep, StorageDep
from app.schemas.common import Page
from app.schemas.error import ErrorResponse
from app.schemas.extraction import ExtractionSummary, PaperPageRead
from app.services.extraction_service import ExtractionService

router = APIRouter(
    prefix="/papers/{paper_id}",
    tags=["extraction"],
    responses={
        401: {"model": ErrorResponse, "description": "Not authenticated"},
        404: {"model": ErrorResponse, "description": "Paper (or page) not found, or owned by another user"},
    },
)


@router.post(
    "/extract",
    response_model=ExtractionSummary,
    summary="Extract the text of every page of one of my papers",
    description=(
        "Runs synchronously. Re-running replaces all previously extracted pages. "
        "Handled outcomes return **200**: `status` is `processed`, or `failed` with a "
        "`failure_code` (`file_missing`, `pdf_corrupted`, `pdf_encrypted`, `too_many_pages`, "
        "`extraction_error`). A fully scanned PDF is `processed` with every page flagged "
        "`needs_ocr`; no OCR is performed."
    ),
    responses={409: {"model": ErrorResponse, "description": "An extraction of this paper is already running"}},
)
async def extract_paper(
    paper_id: uuid.UUID, user: CurrentUser, session: DbSession, storage: StorageDep, settings: SettingsDep
) -> ExtractionSummary:
    return await ExtractionService(session, storage, settings).extract(user.id, paper_id)


@router.get(
    "/pages",
    response_model=Page[PaperPageRead],
    summary="List the extracted pages of one of my papers, in page order",
    description="Empty until an extraction has completed (status `processed`).",
)
async def list_paper_pages(
    paper_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    storage: StorageDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0, le=20_000)] = 0,
) -> Page[PaperPageRead]:
    return await ExtractionService(session, storage, settings).list_pages(
        user.id, paper_id, limit=limit, offset=offset
    )


@router.get(
    "/pages/{page_number}",
    response_model=PaperPageRead,
    summary="Get one extracted page (1-based page number, as printed in the PDF viewer)",
)
async def get_paper_page(
    paper_id: uuid.UUID,
    page_number: Annotated[int, Path(ge=1, le=20_000)],
    user: CurrentUser,
    session: DbSession,
    storage: StorageDep,
    settings: SettingsDep,
) -> PaperPageRead:
    return await ExtractionService(session, storage, settings).get_page(user.id, paper_id, page_number)
