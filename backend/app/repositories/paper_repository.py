"""Data access for papers.

A paper is owned through its workspace. Every method takes owner_id and applies
`papers.workspace_id IN (SELECT id FROM workspaces WHERE owner_id = :owner_id)`
inside the same SQL statement that reads, updates or deletes the row, so there is
no fetch-then-check step and no method that can touch a paper without an owner filter.
"""

import uuid
from typing import Any

from sqlalchemy import ColumnElement, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper import Paper
from app.models.workspace import Workspace


def _owned_by(owner_id: uuid.UUID) -> ColumnElement[bool]:
    owned_workspace_ids = select(Workspace.id).where(Workspace.owner_id == owner_id)
    return Paper.workspace_id.in_(owned_workspace_ids)


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
