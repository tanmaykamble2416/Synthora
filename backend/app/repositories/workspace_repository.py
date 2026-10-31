"""Data access for workspaces.

Every method takes owner_id and puts it in the SQL WHERE clause. There is no method
that reads or writes a workspace without an owner filter, so ownership cannot be
forgotten by a caller, and there is no fetch-then-check race.
"""

import uuid
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.workspace import Workspace


class WorkspaceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, *, owner_id: uuid.UUID, name: str, description: str | None) -> Workspace:
        workspace = Workspace(owner_id=owner_id, name=name, description=description)
        self._session.add(workspace)
        await self._session.flush()
        return workspace

    async def get_owned(self, workspace_id: uuid.UUID, owner_id: uuid.UUID) -> Workspace | None:
        stmt = select(Workspace).where(Workspace.id == workspace_id, Workspace.owner_id == owner_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_owned(
        self, owner_id: uuid.UUID, *, limit: int, offset: int
    ) -> tuple[list[Workspace], int]:
        total_stmt = select(func.count()).select_from(Workspace).where(Workspace.owner_id == owner_id)
        total = (await self._session.execute(total_stmt)).scalar_one()
        stmt = (
            select(Workspace)
            .where(Workspace.owner_id == owner_id)
            .order_by(Workspace.created_at.desc(), Workspace.id.desc())
            .limit(limit)
            .offset(offset)
        )
        items = list((await self._session.execute(stmt)).scalars().all())
        return items, total

    async def update_owned(
        self, workspace_id: uuid.UUID, owner_id: uuid.UUID, values: dict[str, Any]
    ) -> Workspace | None:
        # Single statement: UPDATE ... WHERE id = :id AND owner_id = :owner RETURNING *
        stmt = (
            update(Workspace)
            .where(Workspace.id == workspace_id, Workspace.owner_id == owner_id)
            .values(**values)
            .returning(Workspace)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def delete_owned(self, workspace_id: uuid.UUID, owner_id: uuid.UUID) -> bool:
        stmt = (
            delete(Workspace)
            .where(Workspace.id == workspace_id, Workspace.owner_id == owner_id)
            .returning(Workspace.id)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None
