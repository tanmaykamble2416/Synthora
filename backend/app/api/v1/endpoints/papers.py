import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.api.deps import CurrentUser, DbSession
from app.schemas.common import Page
from app.schemas.error import ErrorResponse
from app.schemas.paper import PaperRead, PaperUpdate
from app.services.paper_service import PaperService

router = APIRouter(
    tags=["papers"],
    responses={401: {"model": ErrorResponse, "description": "Not authenticated"}},
)

NOT_FOUND = {404: {"model": ErrorResponse, "description": "Not found (or owned by another user)"}}


@router.get(
    "/workspaces/{workspace_id}/papers",
    response_model=Page[PaperRead],
    summary="List papers in one of my workspaces (newest first)",
    responses=NOT_FOUND,
)
async def list_papers(
    workspace_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> Page[PaperRead]:
    return await PaperService(session).list_for_workspace(user.id, workspace_id, limit=limit, offset=offset)


@router.get("/papers/{paper_id}", response_model=PaperRead, summary="Get one of my papers", responses=NOT_FOUND)
async def get_paper(paper_id: uuid.UUID, user: CurrentUser, session: DbSession) -> PaperRead:
    paper = await PaperService(session).get(user.id, paper_id)
    return PaperRead.model_validate(paper)


@router.patch(
    "/papers/{paper_id}",
    response_model=PaperRead,
    summary="Update bibliographic metadata (title, abstract, authors, publication_year, doi)",
    responses=NOT_FOUND,
)
async def update_paper(
    paper_id: uuid.UUID, data: PaperUpdate, user: CurrentUser, session: DbSession
) -> PaperRead:
    paper = await PaperService(session).update_metadata(user.id, paper_id, data)
    return PaperRead.model_validate(paper)


@router.delete(
    "/papers/{paper_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a paper record (stored PDF file removal arrives with the Day 4/5 storage service)",
    responses=NOT_FOUND,
)
async def delete_paper(paper_id: uuid.UUID, user: CurrentUser, session: DbSession) -> Response:
    await PaperService(session).delete(user.id, paper_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
