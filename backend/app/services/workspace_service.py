import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.db.errors import is_unique_violation
from app.models.workspace import Workspace
from app.repositories.workspace_repository import WorkspaceRepository
from app.schemas.common import Page
from app.schemas.workspace import WorkspaceCreate, WorkspaceRead, WorkspaceUpdate

NAME_CONSTRAINT = "uq_workspaces_owner_id_lower_name"
NOT_FOUND_MESSAGE = "Workspace not found."
DUPLICATE_MESSAGE = "You already have a workspace with this name."


class WorkspaceService:
    """owner_id always comes from the authenticated user, never from the request body."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._workspaces = WorkspaceRepository(session)

    async def create(self, owner_id: uuid.UUID, data: WorkspaceCreate) -> Workspace:
        try:
            workspace = await self._workspaces.create(
                owner_id=owner_id, name=data.name, description=data.description
            )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            if is_unique_violation(exc, NAME_CONSTRAINT):
                raise ConflictError(DUPLICATE_MESSAGE) from exc
            raise
        return workspace

    async def get(self, owner_id: uuid.UUID, workspace_id: uuid.UUID) -> Workspace:
        workspace = await self._workspaces.get_owned(workspace_id, owner_id)
        if workspace is None:
            raise NotFoundError(NOT_FOUND_MESSAGE)
        return workspace

    async def list(self, owner_id: uuid.UUID, *, limit: int, offset: int) -> Page[WorkspaceRead]:
        items, total = await self._workspaces.list_owned(owner_id, limit=limit, offset=offset)
        return Page[WorkspaceRead](
            items=[WorkspaceRead.model_validate(w) for w in items], total=total, limit=limit, offset=offset
        )

    async def update(
        self, owner_id: uuid.UUID, workspace_id: uuid.UUID, data: WorkspaceUpdate
    ) -> Workspace:
        values = data.model_dump(exclude_unset=True)
        try:
            workspace = await self._workspaces.update_owned(workspace_id, owner_id, values)
            if workspace is None:
                raise NotFoundError(NOT_FOUND_MESSAGE)
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            if is_unique_violation(exc, NAME_CONSTRAINT):
                raise ConflictError(DUPLICATE_MESSAGE) from exc
            raise
        return workspace

    async def delete(self, owner_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
        if not await self._workspaces.delete_owned(workspace_id, owner_id):
            raise NotFoundError(NOT_FOUND_MESSAGE)
        await self._session.commit()
