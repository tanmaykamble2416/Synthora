"""Day 5/6: PDF extraction (pages + structure) and access to page-level text. All routes are owner-only."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.api.deps import CurrentUser, DbSession, GrobidDep, SettingsDep, StorageDep
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
    summary="Extract pages, sections, chunks, references and metadata of one of my papers",
    description=(
        "Runs synchronously. Page text comes from PyMuPDF; sections, references and "
        "bibliographic metadata come from GROBID when it is enabled and reachable. "
        "Otherwise the paper is still processed with page-based chunks "
        "(`structure_source` = `page_fallback`, reason in `grobid_outcome`).\n\n"
        "A successful re-run atomically replaces the previous pages, sections, chunks and "
        "references. A **failed** re-run keeps the previous extraction: `status` = `failed`, "
        "`retained_previous_extraction` = true, `paper_status` = `processed`.\n\n"
        "Metadata is only filled where the paper's field is empty; values you edited are never "
        "overwritten. Chunk sizes are counted in words (`token_count_method` = `word_proxy`).\n\n"
        "Handled outcomes return **200**; `failure_code` is one of `file_missing`, `pdf_corrupted`, "
        "`pdf_encrypted`, `too_many_pages`, `extraction_error`. A fully scanned PDF is `processed` "
        "with every page flagged `needs_ocr`; no OCR is performed."
    ),
    responses={409: {"model": ErrorResponse, "description": "An extraction of this paper is already running"}},
)
async def extract_paper(
    paper_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    storage: StorageDep,
    settings: SettingsDep,
    grobid: GrobidDep,
) -> ExtractionSummary:
    return await ExtractionService(session, storage, settings, grobid).extract(user.id, paper_id)


@router.get(
    "/pages",
    response_model=Page[PaperPageRead],
    summary="List the extracted pages of one of my papers, in page order",
    description="Empty until an extraction has completed. Kept unchanged while a re-extraction runs.",
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
