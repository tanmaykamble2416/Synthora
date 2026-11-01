import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.models.paper import Paper
from app.repositories.paper_repository import PaperRepository
from app.repositories.workspace_repository import WorkspaceRepository
from app.schemas.common import Page
from app.schemas.paper import PaperRead, PaperUpdate

logger = logging.getLogger(__name__)

PAPER_NOT_FOUND = "Paper not found."
WORKSPACE_NOT_FOUND = "Workspace not found."


class PaperService:
    """owner_id always comes from the authenticated user. Papers in workspaces the
    user does not own are reported as 'not found' so their existence is not revealed."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._papers = PaperRepository(session)
        self._workspaces = WorkspaceRepository(session)

    async def list_for_workspace(
        self, owner_id: uuid.UUID, workspace_id: uuid.UUID, *, limit: int, offset: int
    ) -> Page[PaperRead]:
        # SQL-scoped lookup so another user's workspace is a 404, not an empty list.
        if await self._workspaces.get_owned(workspace_id, owner_id) is None:
            raise NotFoundError(WORKSPACE_NOT_FOUND)
        items, total = await self._papers.list_in_workspace(
            workspace_id, owner_id, limit=limit, offset=offset
        )
        return Page[PaperRead](
            items=[PaperRead.model_validate(p) for p in items], total=total, limit=limit, offset=offset
        )

    async def get(self, owner_id: uuid.UUID, paper_id: uuid.UUID) -> Paper:
        paper = await self._papers.get_owned(paper_id, owner_id)
        if paper is None:
            raise NotFoundError(PAPER_NOT_FOUND)
        return paper

    async def update_metadata(self, owner_id: uuid.UUID, paper_id: uuid.UUID, data: PaperUpdate) -> Paper:
        paper = await self._papers.update_owned(paper_id, owner_id, data.to_column_values())
        if paper is None:
            raise NotFoundError(PAPER_NOT_FOUND)
        await self._session.commit()
        return paper

    async def delete(self, owner_id: uuid.UUID, paper_id: uuid.UUID) -> None:
        if not await self._papers.delete_owned(paper_id, owner_id):
            raise NotFoundError(PAPER_NOT_FOUND)
        await self._session.commit()
        # Day 3 limitation: only the database row is removed. Physical file deletion
        # is added with the storage service (Day 4/5); no filesystem path is touched here.
        logger.info("Deleted paper %s (database record only; stored file not yet removed)", paper_id)
