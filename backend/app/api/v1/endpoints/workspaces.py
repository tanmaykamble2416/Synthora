import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.api.deps import CurrentUser, DbSession
from app.schemas.common import Page
from app.schemas.error import ErrorResponse
from app.schemas.workspace import WorkspaceCreate, WorkspaceRead, WorkspaceUpdate
from app.services.workspace_service import WorkspaceService

router = APIRouter(
    prefix="/workspaces",
    tags=["workspaces"],
    responses={401: {"model": ErrorResponse, "description": "Not authenticated"}},
)

NOT_FOUND = {404: {"model": ErrorResponse, "description": "Not found (or owned by another user)"}}
CONFLICT = {409: {"model": ErrorResponse, "description": "Duplicate workspace name"}}


@router.post("", response_model=WorkspaceRead, status_code=status.HTTP_201_CREATED, responses=CONFLICT)
async def create_workspace(data: WorkspaceCreate, user: CurrentUser, session: DbSession) -> WorkspaceRead:
    workspace = await WorkspaceService(session).create(user.id, data)
    return WorkspaceRead.model_validate(workspace)


@router.get("", response_model=Page[WorkspaceRead], summary="List my workspaces (newest first)")
async def list_workspaces(
    user: CurrentUser,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> Page[WorkspaceRead]:
    return await WorkspaceService(session).list(user.id, limit=limit, offset=offset)


@router.get("/{workspace_id}", response_model=WorkspaceRead, responses=NOT_FOUND)
async def get_workspace(workspace_id: uuid.UUID, user: CurrentUser, session: DbSession) -> WorkspaceRead:
    workspace = await WorkspaceService(session).get(user.id, workspace_id)
    return WorkspaceRead.model_validate(workspace)


@router.patch("/{workspace_id}", response_model=WorkspaceRead, responses={**NOT_FOUND, **CONFLICT})
async def update_workspace(
    workspace_id: uuid.UUID, data: WorkspaceUpdate, user: CurrentUser, session: DbSession
) -> WorkspaceRead:
    workspace = await WorkspaceService(session).update(user.id, workspace_id, data)
    return WorkspaceRead.model_validate(workspace)


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT, responses=NOT_FOUND)
async def delete_workspace(workspace_id: uuid.UUID, user: CurrentUser, session: DbSession) -> Response:
    await WorkspaceService(session).delete(user.id, workspace_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
