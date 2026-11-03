"""Orchestrates PDF text extraction for one paper.

Three short database transactions, with the slow PDF work in between and outside
any transaction (no locks or open transactions are held while MuPDF runs):

  T1 claim    UPDATE papers SET status='processing' WHERE <owned, claimable> RETURNING storage_key
              + delete previous pages/sections/chunks                          -> COMMIT
     extract  StorageService.resolve(key) -> extract_pdf(path) in the thread pool
  T2 persist  SELECT ... FOR UPDATE; if still 'processing':
              batched INSERT of all pages + mark processed                      -> COMMIT
  or
  T2 fail     mark failed with a user-safe failure_code                         -> COMMIT

Consequences:
  * A failed or interrupted extraction never leaves old or partial pages behind:
    pages exist only for a paper whose latest extraction completed (status processed).
  * T2 is all-or-nothing; if it fails, nothing of it is visible.
  * Handled PDF problems -> status 'failed' + failure_code, HTTP 200.
    Unexpected errors -> best-effort 'failed'/'extraction_error', then re-raised (HTTP 500).
"""

import logging
import uuid
from datetime import datetime, timedelta

from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ConflictError, NotFoundError
from app.repositories.paper_page_repository import PaperPageRepository
from app.repositories.paper_repository import PaperRepository
from app.schemas.common import Page
from app.schemas.extraction import ExtractionSummary, PaperPageRead
from app.services.pdf_extraction import DocumentExtraction, ExtractionFailure, extract_pdf
from app.services.storage_service import InvalidStorageKeyError, StorageService

logger = logging.getLogger(__name__)

PAPER_NOT_FOUND = "Paper not found."
PAGE_NOT_FOUND = "Page not found."
ALREADY_PROCESSING = "Text extraction is already in progress for this paper."
SUPERSEDED = "This extraction was superseded by another extraction of the same paper."


class ExtractionService:
    def __init__(self, session: AsyncSession, storage: StorageService, settings: Settings) -> None:
        self._session = session
        self._storage = storage
        self._settings = settings
        self._papers = PaperRepository(session)
        self._pages = PaperPageRepository(session)

    # --- reading extracted pages ------------------------------------------------------

    async def list_pages(
        self, owner_id: uuid.UUID, paper_id: uuid.UUID, *, limit: int, offset: int
    ) -> Page[PaperPageRead]:
        await self._require_owned_paper(owner_id, paper_id)
        items, total = await self._pages.list_owned(paper_id, owner_id, limit=limit, offset=offset)
        return Page[PaperPageRead](
            items=[PaperPageRead.model_validate(p) for p in items], total=total, limit=limit, offset=offset
        )

    async def get_page(self, owner_id: uuid.UUID, paper_id: uuid.UUID, page_number: int) -> PaperPageRead:
        await self._require_owned_paper(owner_id, paper_id)
        page = await self._pages.get_owned(paper_id, owner_id, page_number)
        if page is None:
            raise NotFoundError(PAGE_NOT_FOUND)
        return PaperPageRead.model_validate(page)

    async def _require_owned_paper(self, owner_id: uuid.UUID, paper_id: uuid.UUID) -> None:
        if await self._papers.get_owned(paper_id, owner_id) is None:
            raise NotFoundError(PAPER_NOT_FOUND)

    # --- running an extraction --------------------------------------------------------

    async def extract(self, owner_id: uuid.UUID, paper_id: uuid.UUID) -> ExtractionSummary:
        storage_key = await self._claim(owner_id, paper_id)
        try:
            result = await run_in_threadpool(self._run_engine, storage_key)
        except ExtractionFailure as failure:
            return await self._record_failure(owner_id, paper_id, failure)
        except Exception:
            # Unexpected (a bug, not a bad PDF): do not leave the paper in 'processing'.
            logger.exception("Unexpected error while extracting paper %s", paper_id)
            await self._record_failure_quietly(owner_id, paper_id, "unexpected engine error")
            raise
        return await self._persist(owner_id, paper_id, result)

    # --- T1 --------------------------------------------------------------------------

    async def _claim(self, owner_id: uuid.UUID, paper_id: uuid.UUID) -> str:
        stale_after = timedelta(minutes=self._settings.extraction_stale_minutes)
        storage_key = await self._papers.claim_for_extraction(paper_id, owner_id, stale_after=stale_after)
        if storage_key is None:
            await self._session.rollback()
            if await self._papers.get_owned(paper_id, owner_id) is None:
                raise NotFoundError(PAPER_NOT_FOUND)
            raise ConflictError(ALREADY_PROCESSING)
        await self._papers.delete_extracted_content(paper_id, owner_id)
        await self._session.commit()
        logger.info("Extraction started for paper %s", paper_id)
        return storage_key

    # --- outside any transaction ------------------------------------------------------

    def _run_engine(self, storage_key: str) -> DocumentExtraction:
        try:
            path = self._storage.resolve(storage_key)  # never a client-supplied path
        except InvalidStorageKeyError as exc:
            raise ExtractionFailure("file_missing", "storage key is not a server-generated key") from exc
        return extract_pdf(path, max_pages=self._settings.extraction_max_pages)

    # --- T2 --------------------------------------------------------------------------

    async def _persist(self, owner_id: uuid.UUID, paper_id: uuid.UUID, result: DocumentExtraction) -> ExtractionSummary:
        try:
            status = await self._papers.lock_status_for_update(paper_id, owner_id)
            if status != "processing":
                # Paper deleted, or a later (stale-recovery) extraction already finished.
                await self._session.rollback()
                logger.warning("Discarding extraction result for paper %s (status now %s)", paper_id, status)
                if status is None:
                    raise NotFoundError(PAPER_NOT_FOUND)
                raise ConflictError(SUPERSEDED)
            await self._pages.insert_pages(paper_id, result.pages)
            processed_at = await self._papers.mark_processed(paper_id, owner_id, page_count=result.page_count)
            await self._session.commit()
        except (NotFoundError, ConflictError):
            raise
        except Exception:
            await self._session.rollback()  # no page of this attempt survives
            logger.exception("Could not store extracted pages for paper %s", paper_id)
            await self._record_failure_quietly(owner_id, paper_id, "persisting pages failed")
            raise
        logger.info(
            "Extraction finished for paper %s: %d pages, %d need OCR",
            paper_id, result.page_count, result.needs_ocr_pages,
        )
        return _summary(paper_id, "processed", None, result, processed_at)

    async def _record_failure(
        self, owner_id: uuid.UUID, paper_id: uuid.UUID, failure: ExtractionFailure
    ) -> ExtractionSummary:
        # failure.detail is internal (stored in error_message, logged); never returned.
        logger.warning("Extraction failed for paper %s: %s (%s)", paper_id, failure.code, failure.detail)
        processed_at = await self._papers.mark_failed(
            paper_id, owner_id, failure_code=failure.code, internal_message=failure.detail
        )
        await self._session.commit()
        if processed_at is None:  # no longer 'processing': deleted, or superseded
            if await self._papers.get_owned(paper_id, owner_id) is None:
                raise NotFoundError(PAPER_NOT_FOUND)
            raise ConflictError(SUPERSEDED)
        return _summary(paper_id, "failed", failure.code, None, processed_at)

    async def _record_failure_quietly(self, owner_id: uuid.UUID, paper_id: uuid.UUID, detail: str) -> None:
        try:
            await self._session.rollback()
            await self._papers.mark_failed(
                paper_id, owner_id, failure_code="extraction_error", internal_message=detail
            )
            await self._session.commit()
        except Exception:
            logger.exception(
                "Could not mark paper %s as failed; it will be reclaimable after %d minutes",
                paper_id, self._settings.extraction_stale_minutes,
            )


def _summary(
    paper_id: uuid.UUID,
    status: str,
    failure_code: str | None,
    result: DocumentExtraction | None,
    processed_at: datetime | None,
) -> ExtractionSummary:
    if result is None:
        return ExtractionSummary(
            paper_id=paper_id, status=status, failure_code=failure_code, page_count=0, text_pages=0,
            low_text_pages=0, empty_pages=0, needs_ocr_pages=0, total_characters=0, total_words=0,
            processed_at=processed_at,
        )
    return ExtractionSummary(
        paper_id=paper_id,
        status=status,
        failure_code=failure_code,
        page_count=result.page_count,
        text_pages=result.count("text"),
        low_text_pages=result.count("low_text"),
        empty_pages=result.count("empty"),
        needs_ocr_pages=result.needs_ocr_pages,
        total_characters=result.total_characters,
        total_words=result.total_words,
        processed_at=processed_at,
    )
