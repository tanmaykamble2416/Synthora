"""Data access for extracted pages. Reads are owner-scoped through the parent paper."""

import uuid
from collections.abc import Sequence

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_page import PaperPage
from app.repositories.paper_repository import owned_paper_ids
from app.services.pdf_extraction import PageExtraction


class PaperPageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_pages(self, paper_id: uuid.UUID, pages: Sequence[PageExtraction]) -> None:
        """One batched INSERT for all pages (no query per page). The caller has already
        deleted previous pages in the same transaction; UNIQUE (paper_id, page_number)
        makes duplicates impossible anyway."""
        if not pages:
            return
        rows = [
            {
                "paper_id": paper_id,
                "page_number": p.page_number,
                "text": p.text,
                "char_count": p.char_count,
                "word_count": p.word_count,
                "extraction_status": p.extraction_status,
                "needs_ocr": p.needs_ocr,
            }
            for p in pages
        ]
        await self._session.execute(insert(PaperPage), rows)

    async def list_owned(
        self, paper_id: uuid.UUID, owner_id: uuid.UUID, *, limit: int, offset: int
    ) -> tuple[list[PaperPage], int]:
        criteria = (PaperPage.paper_id == paper_id, PaperPage.paper_id.in_(owned_paper_ids(owner_id)))
        total = (await self._session.execute(select(func.count()).select_from(PaperPage).where(*criteria))).scalar_one()
        stmt = select(PaperPage).where(*criteria).order_by(PaperPage.page_number).limit(limit).offset(offset)
        return list((await self._session.execute(stmt)).scalars().all()), total

    async def get_owned(self, paper_id: uuid.UUID, owner_id: uuid.UUID, page_number: int) -> PaperPage | None:
        stmt = select(PaperPage).where(
            PaperPage.paper_id == paper_id,
            PaperPage.page_number == page_number,
            PaperPage.paper_id.in_(owned_paper_ids(owner_id)),
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()
