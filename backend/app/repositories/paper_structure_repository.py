"""Data access for sections, chunks and references (Day 6).

Writes happen only inside T2 of an extraction, after delete_extracted_content, so
inserts never collide with a previous extraction's rows. Reads are owner-scoped
through the parent paper, like pages.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_chunk import PaperChunk
from app.models.paper_reference import PaperReference
from app.models.paper_section import PaperSection
from app.repositories.paper_repository import owned_paper_ids
from app.services.chunker import ChunkDraft, SectionDraft
from app.services.tei_parser import TeiReference


class PaperStructureRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- writes (T2 only) -------------------------------------------------------------

    async def insert_sections(self, paper_id: uuid.UUID, sections: Sequence[SectionDraft]) -> dict[int, uuid.UUID]:
        """One batched INSERT. Ids are generated here so chunks can reference their
        section without a round trip; returns {order_index: section_id}."""
        ids = {s.order_index: uuid.uuid4() for s in sections}
        if sections:
            rows = [
                {
                    "id": ids[s.order_index],
                    "paper_id": paper_id,
                    "order_index": s.order_index,
                    "level": s.level,
                    "heading": s.heading,
                    "section_number": s.section_number,
                    "section_type": s.section_type,
                    "text": s.text,
                    "page_start": s.page_start,
                    "page_end": s.page_end,
                }
                for s in sections
            ]
            await self._session.execute(insert(PaperSection), rows)
        return ids

    async def insert_chunks(
        self,
        paper_id: uuid.UUID,
        workspace_id: uuid.UUID,
        chunks: Sequence[ChunkDraft],
        section_ids: dict[int, uuid.UUID],
    ) -> None:
        if not chunks:
            return
        rows = [
            {
                "paper_id": paper_id,
                "workspace_id": workspace_id,
                "section_id": None if c.section_order_index is None else section_ids[c.section_order_index],
                "chunk_index": c.chunk_index,
                "text": c.text,
                "token_count": c.token_count,
                "token_count_method": c.token_count_method,
                "content_type": c.content_type,
                "page_start": c.page_start,
                "page_end": c.page_end,
            }
            for c in chunks
        ]
        await self._session.execute(insert(PaperChunk), rows)

    async def insert_references(self, paper_id: uuid.UUID, references: Sequence[TeiReference]) -> None:
        if not references:
            return
        rows = [
            {
                "paper_id": paper_id,
                "ref_index": index,
                "raw_text": r.raw_text,
                "title": r.title,
                "authors": list(r.authors) or None,
                "publication_year": r.publication_year,
                "venue": r.venue,
                "doi": r.doi,
                "page_start": r.page_start,
                "page_end": r.page_end,
            }
            for index, r in enumerate(references)
        ]
        await self._session.execute(insert(PaperReference), rows)

    # --- owner-scoped reads -------------------------------------------------------------

    async def list_sections_owned(
        self, paper_id: uuid.UUID, owner_id: uuid.UUID, *, limit: int, offset: int
    ) -> tuple[list[PaperSection], int]:
        criteria = (PaperSection.paper_id == paper_id, PaperSection.paper_id.in_(owned_paper_ids(owner_id)))
        total = await self._count(PaperSection, criteria)
        stmt = select(PaperSection).where(*criteria).order_by(PaperSection.order_index).limit(limit).offset(offset)
        return list((await self._session.execute(stmt)).scalars().all()), total

    async def list_chunks_owned(
        self, paper_id: uuid.UUID, owner_id: uuid.UUID, *, limit: int, offset: int
    ) -> tuple[list[dict[str, Any]], int]:
        """Chunks in reading order with their section heading (NULL for fallback chunks)."""
        criteria = (PaperChunk.paper_id == paper_id, PaperChunk.paper_id.in_(owned_paper_ids(owner_id)))
        total = await self._count(PaperChunk, criteria)
        stmt = (
            select(
                PaperChunk.chunk_index,
                PaperChunk.section_id,
                PaperSection.heading.label("section_heading"),
                PaperSection.section_number.label("section_number"),
                PaperSection.section_type.label("section_type"),
                PaperChunk.text,
                PaperChunk.token_count,
                PaperChunk.token_count_method,
                PaperChunk.content_type,
                PaperChunk.page_start,
                PaperChunk.page_end,
            )
            .outerjoin(
                PaperSection,
                (PaperSection.id == PaperChunk.section_id) & (PaperSection.paper_id == PaperChunk.paper_id),
            )
            .where(*criteria)
            .order_by(PaperChunk.chunk_index)
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._session.execute(stmt)).mappings().all()
        return [dict(r) for r in rows], total

    async def list_references_owned(
        self, paper_id: uuid.UUID, owner_id: uuid.UUID, *, limit: int, offset: int
    ) -> tuple[list[PaperReference], int]:
        criteria = (PaperReference.paper_id == paper_id, PaperReference.paper_id.in_(owned_paper_ids(owner_id)))
        total = await self._count(PaperReference, criteria)
        stmt = (
            select(PaperReference).where(*criteria).order_by(PaperReference.ref_index).limit(limit).offset(offset)
        )
        return list((await self._session.execute(stmt)).scalars().all()), total

    async def _count(self, model: type, criteria: tuple) -> int:
        stmt = select(func.count()).select_from(model).where(*criteria)
        return (await self._session.execute(stmt)).scalar_one()
