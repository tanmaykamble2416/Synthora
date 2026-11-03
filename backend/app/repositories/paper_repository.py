"""Data access for papers.

A paper is owned through its workspace. Every method takes owner_id and applies
`papers.workspace_id IN (SELECT id FROM workspaces WHERE owner_id = :owner_id)`
inside the same SQL statement that reads, updates or deletes the row, so there is
no fetch-then-check step and no method that can touch a paper without an owner filter.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, Select, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper import Paper
from app.models.paper_chunk import PaperChunk
from app.models.paper_page import PaperPage
from app.models.paper_section import PaperSection
from app.models.workspace import Workspace

# Statuses from which a new extraction may start ("processing" only once stale).
CLAIMABLE_STATUSES = ("uploaded", "processed", "failed")


def _owned_by(owner_id: uuid.UUID) -> ColumnElement[bool]:
    owned_workspace_ids = select(Workspace.id).where(Workspace.owner_id == owner_id)
    return Paper.workspace_id.in_(owned_workspace_ids)


def owned_paper_ids(owner_id: uuid.UUID) -> Select[tuple[uuid.UUID]]:
    """Sub-select of the ids of every paper the owner may access (for child tables)."""
    return select(Paper.id).where(_owned_by(owner_id))


class PaperRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_owned(self, paper_id: uuid.UUID, owner_id: uuid.UUID) -> Paper | None:
        stmt = select(Paper).where(Paper.id == paper_id, _owned_by(owner_id))
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_in_workspace(
        self, workspace_id: uuid.UUID, owner_id: uuid.UUID, *, limit: int, offset: int
    ) -> tuple[list[Paper], int]:
        criteria = (Paper.workspace_id == workspace_id, _owned_by(owner_id))
        total_stmt = select(func.count()).select_from(Paper).where(*criteria)
        total = (await self._session.execute(total_stmt)).scalar_one()
        stmt = (
            select(Paper)
            .where(*criteria)
            # id breaks ties between papers created in the same instant -> deterministic pages.
            .order_by(Paper.created_at.desc(), Paper.id.desc())
            .limit(limit)
            .offset(offset)
        )
        items = list((await self._session.execute(stmt)).scalars().all())
        return items, total

    async def update_owned(
        self, paper_id: uuid.UUID, owner_id: uuid.UUID, values: dict[str, Any]
    ) -> Paper | None:
        # Single statement: UPDATE papers SET ... WHERE id = :id AND <owned> RETURNING *
        stmt = (
            update(Paper)
            .where(Paper.id == paper_id, _owned_by(owner_id))
            .values(**values)
            .returning(Paper)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def delete_owned(self, paper_id: uuid.UUID, owner_id: uuid.UUID) -> str | None:
        """Delete the paper row; return its storage_key, or None if not found/not owned.
        Sections and chunks are removed by ON DELETE CASCADE."""
        stmt = delete(Paper).where(Paper.id == paper_id, _owned_by(owner_id)).returning(Paper.storage_key)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def sha256_exists_in_owned_workspace(
        self, workspace_id: uuid.UUID, owner_id: uuid.UUID, sha256: str
    ) -> bool:
        """Fast duplicate pre-check. The UNIQUE (workspace_id, sha256) constraint remains
        the real guarantee for concurrent uploads."""
        stmt = select(
            select(Paper.id)
            .where(Paper.workspace_id == workspace_id, Paper.sha256 == sha256, _owned_by(owner_id))
            .exists()
        )
        return bool((await self._session.execute(stmt)).scalar_one())

    async def create(
        self,
        *,
        workspace_id: uuid.UUID,
        original_filename: str,
        storage_key: str,
        sha256: str,
        file_size_bytes: int,
    ) -> Paper:
        # Status defaults to 'uploaded'; page_count and bibliographic metadata stay NULL
        # until extraction (Day 5). Nothing is guessed.
        paper = Paper(
            workspace_id=workspace_id,
            original_filename=original_filename,
            storage_key=storage_key,
            sha256=sha256,
            file_size_bytes=file_size_bytes,
        )
        self._session.add(paper)
        await self._session.flush()
        return paper

    # --- extraction lifecycle (Day 5) ----------------------------------------------
    # Every statement still carries the owner filter.

    async def claim_for_extraction(
        self, paper_id: uuid.UUID, owner_id: uuid.UUID, *, stale_after: timedelta
    ) -> str | None:
        """Atomically move the paper to 'processing'. Returns its storage_key, or None if
        it is not owned/not found or another extraction is already running (and not stale).

        A single conditional UPDATE acts as the lock: two concurrent requests cannot both
        claim the same paper. updated_at is refreshed by this UPDATE (ORM onupdate), so
        it records when processing started for stale-claim recovery. The cutoff is
        computed by PostgreSQL (now() - interval), so app/DB clock skew cannot matter."""
        stmt = (
            update(Paper)
            .where(
                Paper.id == paper_id,
                _owned_by(owner_id),
                or_(
                    Paper.status.in_(CLAIMABLE_STATUSES),
                    (Paper.status == "processing") & (Paper.updated_at < func.now() - stale_after),
                ),
            )
            .values(status="processing", failure_code=None, error_message=None)
            .returning(Paper.storage_key)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def delete_extracted_content(self, paper_id: uuid.UUID, owner_id: uuid.UUID) -> None:
        """Remove pages and everything derived from them (sections, chunks), so stale
        content can never be shown next to a new extraction."""
        owned = Paper.id.in_(owned_paper_ids(owner_id))
        target = select(Paper.id).where(Paper.id == paper_id, owned)
        for model in (PaperChunk, PaperSection, PaperPage):  # chunks reference sections
            await self._session.execute(delete(model).where(model.paper_id.in_(target)))

    async def lock_status_for_update(self, paper_id: uuid.UUID, owner_id: uuid.UUID) -> str | None:
        """SELECT status ... FOR UPDATE: serialises the final write of concurrent extractions."""
        stmt = select(Paper.status).where(Paper.id == paper_id, _owned_by(owner_id)).with_for_update()
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def mark_processed(self, paper_id: uuid.UUID, owner_id: uuid.UUID, *, page_count: int) -> datetime | None:
        stmt = (
            update(Paper)
            .where(Paper.id == paper_id, _owned_by(owner_id), Paper.status == "processing")
            .values(
                status="processed", page_count=page_count, processed_at=func.now(),
                failure_code=None, error_message=None,
            )
            .returning(Paper.processed_at)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def mark_failed(
        self, paper_id: uuid.UUID, owner_id: uuid.UUID, *, failure_code: str, internal_message: str
    ) -> datetime | None:
        stmt = (
            update(Paper)
            .where(Paper.id == paper_id, _owned_by(owner_id), Paper.status == "processing")
            .values(
                status="failed", failure_code=failure_code, error_message=internal_message,
                page_count=None, processed_at=func.now(),
            )
            .returning(Paper.processed_at)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()
