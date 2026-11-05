"""Orchestrates extraction for one paper: pages (PyMuPDF), structure (GROBID or page
fallback), chunks, references and metadata.

Day 6 keeps the LAST GOOD EXTRACTION. Short database transactions, with all slow
work in between and outside any transaction:

  T1 claim     UPDATE papers SET status='processing', extraction_claim_id=<new uuid>
               WHERE <owned, claimable>                                           -> COMMIT
               Nothing is deleted: the previous pages/sections/chunks/references
               stay readable while the new extraction runs.
     work      PyMuPDF (thread pool, under the MuPDF lock)
               -> GROBID over HTTP (optional; outside the MuPDF lock)
               -> TEI parsing + chunking (thread pool)
  T2 replace   SELECT ... FOR UPDATE; only if still 'processing' with OUR claim id:
               delete previous content, insert pages + sections + chunks + references,
               fill NULL metadata, mark processed                                 -> COMMIT
               All or nothing: on any error T2 is rolled back, so the previous
               extraction is still intact, and T3 records the failure.
  T3 failure   (separate transaction, only with OUR claim id) status -> 'processed' if a previous extraction
               exists (it is kept untouched), else 'failed' + failure_code;
               last_extraction_failure_code / last_extraction_attempt_at record the
               attempt; processed_at keeps the time of the last success.     -> COMMIT

Claim fencing: if this attempt was declared stale and another request reclaimed the
paper (stale recovery), the claim id no longer matches. This attempt then writes
NOTHING: T2 discards its result (409) and T3 records nothing, so the newer attempt
alone decides the outcome.

GROBID never decides success: if it is disabled, unreachable, slow or returns
unusable TEI, the extraction still succeeds with structure_source='page_fallback'
and the reason in grobid_outcome. Metadata and references are applied ONLY when
grobid_outcome = 'used'.

Handled PDF problems -> HTTP 200 with status 'failed' + failure_code.
Unexpected errors -> best-effort T3 with 'extraction_error', then re-raised (HTTP 500).
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ConflictError, NotFoundError
from app.repositories.paper_page_repository import PaperPageRepository
from app.repositories.paper_repository import ClaimedPaper, PaperRepository
from app.repositories.paper_structure_repository import PaperStructureRepository
from app.schemas.common import Page
from app.schemas.extraction import ExtractionSummary, PaperPageRead
from app.schemas.structure import PaperChunkRead, PaperReferenceRead, PaperSectionRead
from app.services.chunker import ChunkingConfig, StructureDrafts, build_from_pages, build_from_tei
from app.services.grobid_client import GrobidClient
from app.services.metadata_merge import MERGEABLE_FIELDS, fill_only_null, proposal_from_header
from app.services.pdf_extraction import DocumentExtraction, ExtractionFailure, detect_table_regions, extract_pdf
from app.services.storage_service import InvalidStorageKeyError, StorageService
from app.services.structure_repair import repair_structure
from app.services.tei_parser import TeiDocument, TeiParseError, TeiReference, TeiSection, UnsafeXmlParserError, parse_tei
from app.services.token_counting import load_token_counter

logger = logging.getLogger(__name__)

PAPER_NOT_FOUND = "Paper not found."
PAGE_NOT_FOUND = "Page not found."
ALREADY_PROCESSING = "Text extraction is already in progress for this paper."
SUPERSEDED = "This extraction was superseded by another extraction of the same paper."


@dataclass(frozen=True, slots=True)
class DocumentStructure:
    structure_source: str  # "grobid" | "page_fallback"
    grobid_outcome: str  # "used" | "disabled" | "unavailable" | "timeout" | "invalid_response" | "error"
    drafts: StructureDrafts
    references: tuple[TeiReference, ...]
    metadata_proposal: dict[str, Any]


class ExtractionService:
    def __init__(
        self,
        session: AsyncSession,
        storage: StorageService,
        settings: Settings,
        grobid: GrobidClient | None = None,
    ) -> None:
        self._session = session
        self._storage = storage
        self._settings = settings
        self._grobid = grobid  # None = GROBID disabled
        self._papers = PaperRepository(session)
        self._pages = PaperPageRepository(session)
        self._structure = PaperStructureRepository(session)
        self._chunking = ChunkingConfig(
            target_words=settings.chunk_target_words,
            max_words=settings.chunk_max_words,
            min_words=settings.chunk_min_words,
            counter=load_token_counter(
                settings.chunk_token_counter, settings.chunk_tokenizer_file, settings.chunk_tokenizer_name
            ),
        )

    # --- reading extracted content ----------------------------------------------------

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

    async def list_sections(
        self, owner_id: uuid.UUID, paper_id: uuid.UUID, *, limit: int, offset: int
    ) -> Page[PaperSectionRead]:
        await self._require_owned_paper(owner_id, paper_id)
        items, total = await self._structure.list_sections_owned(paper_id, owner_id, limit=limit, offset=offset)
        return Page[PaperSectionRead](
            items=[PaperSectionRead.model_validate(s) for s in items], total=total, limit=limit, offset=offset
        )

    async def list_chunks(
        self, owner_id: uuid.UUID, paper_id: uuid.UUID, *, limit: int, offset: int
    ) -> Page[PaperChunkRead]:
        await self._require_owned_paper(owner_id, paper_id)
        items, total = await self._structure.list_chunks_owned(paper_id, owner_id, limit=limit, offset=offset)
        return Page[PaperChunkRead](
            items=[PaperChunkRead.model_validate(c) for c in items], total=total, limit=limit, offset=offset
        )

    async def list_references(
        self, owner_id: uuid.UUID, paper_id: uuid.UUID, *, limit: int, offset: int
    ) -> Page[PaperReferenceRead]:
        await self._require_owned_paper(owner_id, paper_id)
        items, total = await self._structure.list_references_owned(paper_id, owner_id, limit=limit, offset=offset)
        return Page[PaperReferenceRead](
            items=[PaperReferenceRead.model_validate(r) for r in items], total=total, limit=limit, offset=offset
        )

    async def _require_owned_paper(self, owner_id: uuid.UUID, paper_id: uuid.UUID) -> None:
        if await self._papers.get_owned(paper_id, owner_id) is None:
            raise NotFoundError(PAPER_NOT_FOUND)

    # --- running an extraction --------------------------------------------------------

    async def extract(self, owner_id: uuid.UUID, paper_id: uuid.UUID) -> ExtractionSummary:
        claimed = await self._claim(owner_id, paper_id)
        try:
            path, result = await run_in_threadpool(self._run_engine, claimed.storage_key)
        except ExtractionFailure as failure:
            return await self._record_failure(owner_id, paper_id, claimed.claim_id, failure)
        except Exception:
            # Unexpected (a bug, not a bad PDF): do not leave the paper in 'processing'.
            logger.exception("Unexpected error while extracting paper %s", paper_id)
            await self._record_failure_quietly(owner_id, paper_id, claimed.claim_id, "unexpected engine error")
            raise
        try:
            structure = await self._build_structure(paper_id, path, result)
        except Exception:
            logger.exception("Unexpected error while building the structure of paper %s", paper_id)
            await self._record_failure_quietly(owner_id, paper_id, claimed.claim_id, "unexpected structure error")
            raise
        return await self._persist(owner_id, paper_id, claimed.claim_id, result, structure)

    # --- T1 --------------------------------------------------------------------------

    async def _claim(self, owner_id: uuid.UUID, paper_id: uuid.UUID) -> ClaimedPaper:
        stale_after = timedelta(minutes=self._settings.extraction_stale_minutes)
        claimed = await self._papers.claim_for_extraction(paper_id, owner_id, stale_after=stale_after)
        if claimed is None:
            await self._session.rollback()
            if await self._papers.get_owned(paper_id, owner_id) is None:
                raise NotFoundError(PAPER_NOT_FOUND)
            raise ConflictError(ALREADY_PROCESSING)
        await self._session.commit()  # status only: previous content is NOT deleted here
        logger.info("Extraction started for paper %s", paper_id)
        return claimed

    # --- outside any transaction ------------------------------------------------------

    def _run_engine(self, storage_key: str) -> tuple[Path, DocumentExtraction]:
        try:
            path = self._storage.resolve(storage_key)  # never a client-supplied path
        except InvalidStorageKeyError as exc:
            raise ExtractionFailure("file_missing", "storage key is not a server-generated key") from exc
        return path, extract_pdf(path, max_pages=self._settings.extraction_max_pages)

    async def _build_structure(
        self, paper_id: uuid.UUID, path: Path, result: DocumentExtraction
    ) -> DocumentStructure:
        page_texts = [p.text for p in result.pages]
        outcome = "disabled"
        document = None
        if self._grobid is not None:
            response = await self._grobid.process_fulltext(path)
            outcome = response.outcome
            if response.outcome == "ok" and response.tei is not None:
                try:
                    document = await run_in_threadpool(parse_tei, response.tei, page_count=result.page_count)
                    outcome = "used"
                except UnsafeXmlParserError as exc:
                    logger.error("Refusing to parse GROBID output for paper %s: %s", paper_id, exc)
                    outcome = "error"
                except TeiParseError as exc:
                    logger.warning("Unusable GROBID output for paper %s: %s", paper_id, exc)
                    outcome = "invalid_response"

        if document is not None:
            sections = await self._repaired_sections(paper_id, path, document, page_texts)
            try:
                drafts = await run_in_threadpool(build_from_tei, sections, page_texts, self._chunking)
            except Exception:
                logger.exception("Could not build sections from GROBID output for paper %s", paper_id)
                outcome = "error"
            else:
                if drafts.unlocated_paragraphs:
                    logger.info(
                        "Paper %s: %d paragraphs had no page coordinates and no text match; "
                        "they were given the page of the preceding paragraph",
                        paper_id, drafts.unlocated_paragraphs,
                    )
                return DocumentStructure(
                    structure_source="grobid",
                    grobid_outcome="used",
                    drafts=drafts,
                    references=document.references,
                    metadata_proposal=proposal_from_header(document.header),
                )

        drafts = await run_in_threadpool(build_from_pages, page_texts, self._chunking)
        return DocumentStructure(
            structure_source="page_fallback",
            grobid_outcome=outcome,
            drafts=drafts,
            references=(),  # the fallback never invents references or metadata
            metadata_proposal={},
        )

    async def _repaired_sections(
        self, paper_id: uuid.UUID, path: Path, document: TeiDocument, page_texts: list[str]
    ) -> list[TeiSection]:
        """GROBID's sections corrected with PDF evidence (structure_repair). Best effort:
        any problem leaves GROBID's own structure, it never fails the extraction."""
        try:
            regions: list = []
            if self._settings.extraction_detect_tables:
                regions = await run_in_threadpool(detect_table_regions, path, page_count=len(page_texts))
            result = await run_in_threadpool(
                lambda: repair_structure(document.sections, page_texts, figures=document.figures, table_regions=regions)
            )
            return result.sections
        except Exception:
            logger.exception("Structure repair failed for paper %s; using GROBID's structure as is", paper_id)
            return list(document.sections)

    # --- T2 --------------------------------------------------------------------------

    async def _persist(
        self,
        owner_id: uuid.UUID,
        paper_id: uuid.UUID,
        claim_id: uuid.UUID,
        result: DocumentExtraction,
        structure: DocumentStructure,
    ) -> ExtractionSummary:
        try:
            locked = await self._papers.lock_for_persist(paper_id, owner_id)
            if locked is None or not locked.is_held_by(claim_id):
                # Paper deleted, or this attempt was superseded by a stale-recovery claim
                # (which may still be running, or may already have finished). Nothing of
                # this attempt is written.
                await self._session.rollback()
                logger.warning(
                    "Discarding extraction result for paper %s (status now %s)",
                    paper_id, None if locked is None else locked.status,
                )
                if locked is None:
                    raise NotFoundError(PAPER_NOT_FOUND)
                raise ConflictError(SUPERSEDED)
            await self._papers.delete_extracted_content(paper_id, owner_id)
            await self._pages.insert_pages(paper_id, result.pages)
            section_ids = await self._structure.insert_sections(paper_id, structure.drafts.sections)
            await self._structure.insert_chunks(paper_id, locked.workspace_id, structure.drafts.chunks, section_ids)
            await self._structure.insert_references(paper_id, structure.references)
            filled = fill_only_null(locked.metadata, structure.metadata_proposal)
            await self._papers.fill_metadata(paper_id, owner_id, filled)
            processed_at = await self._papers.mark_processed(
                paper_id,
                owner_id,
                claim_id=claim_id,
                page_count=result.page_count,
                structure_source=structure.structure_source,
                grobid_outcome=structure.grobid_outcome,
            )
            if processed_at is None:  # impossible while the row lock is held; never commit half a claim
                await self._session.rollback()
                raise ConflictError(SUPERSEDED)
            await self._session.commit()
        except (NotFoundError, ConflictError):
            raise
        except Exception:
            await self._session.rollback()  # the previous extraction is untouched
            logger.exception("Could not store the extraction of paper %s", paper_id)
            await self._record_failure_quietly(owner_id, paper_id, claim_id, "persisting extraction failed")
            raise
        logger.info(
            "Extraction finished for paper %s: %d pages (%d need OCR), %s/%s, %d sections, %d chunks, "
            "%d references, metadata filled: %s",
            paper_id, result.page_count, result.needs_ocr_pages, structure.structure_source,
            structure.grobid_outcome, len(structure.drafts.sections), len(structure.drafts.chunks),
            len(structure.references), ",".join(sorted(filled)) or "none",
        )
        return ExtractionSummary(
            paper_id=paper_id,
            status="processed",
            paper_status="processed",
            failure_code=None,
            retained_previous_extraction=False,
            page_count=result.page_count,
            text_pages=result.count("text"),
            low_text_pages=result.count("low_text"),
            empty_pages=result.count("empty"),
            needs_ocr_pages=result.needs_ocr_pages,
            total_characters=result.total_characters,
            total_words=result.total_words,
            structure_source=structure.structure_source,
            grobid_outcome=structure.grobid_outcome,
            section_count=len(structure.drafts.sections),
            chunk_count=len(structure.drafts.chunks),
            reference_count=len(structure.references),
            metadata_fields_filled=[f for f in MERGEABLE_FIELDS if f in filled],
            processed_at=processed_at,
        )

    # --- T3 --------------------------------------------------------------------------

    async def _record_failure(
        self, owner_id: uuid.UUID, paper_id: uuid.UUID, claim_id: uuid.UUID, failure: ExtractionFailure
    ) -> ExtractionSummary:
        # failure.detail is internal (stored in error_message, logged); never returned.
        logger.warning("Extraction failed for paper %s: %s (%s)", paper_id, failure.code, failure.detail)
        recorded = await self._papers.record_failed_attempt(
            paper_id, owner_id, claim_id=claim_id, failure_code=failure.code, internal_message=failure.detail
        )
        await self._session.commit()
        if recorded is None:  # our claim is gone: paper deleted, or superseded by a newer claim
            if await self._papers.get_owned(paper_id, owner_id) is None:
                raise NotFoundError(PAPER_NOT_FOUND)
            raise ConflictError(SUPERSEDED)
        retained = recorded.status == "processed"
        if retained:
            logger.info("Paper %s keeps its previous extraction after the failed attempt", paper_id)
        return _failed_summary(paper_id, failure.code, retained, recorded.processed_at)

    async def _record_failure_quietly(
        self, owner_id: uuid.UUID, paper_id: uuid.UUID, claim_id: uuid.UUID, detail: str
    ) -> None:
        """T3 for unexpected errors: a NEW transaction after rolling back whatever failed.
        Records nothing if this attempt no longer holds the claim."""
        try:
            await self._session.rollback()
            recorded = await self._papers.record_failed_attempt(
                paper_id, owner_id, claim_id=claim_id, failure_code="extraction_error", internal_message=detail
            )
            await self._session.commit()
            if recorded is None:
                logger.warning("Not recording the failure of paper %s: this attempt was superseded", paper_id)
        except Exception:
            logger.exception(
                "Could not record the failed extraction of paper %s; it will be reclaimable after %d minutes",
                paper_id, self._settings.extraction_stale_minutes,
            )


def _failed_summary(
    paper_id: uuid.UUID, failure_code: str, retained: bool, processed_at: datetime | None
) -> ExtractionSummary:
    return ExtractionSummary(
        paper_id=paper_id,
        status="failed",
        paper_status="processed" if retained else "failed",
        failure_code=failure_code,
        retained_previous_extraction=retained,
        page_count=0,
        text_pages=0,
        low_text_pages=0,
        empty_pages=0,
        needs_ocr_pages=0,
        total_characters=0,
        total_words=0,
        structure_source=None,
        grobid_outcome=None,
        section_count=0,
        chunk_count=0,
        reference_count=0,
        metadata_fields_filled=[],
        processed_at=processed_at,
    )
