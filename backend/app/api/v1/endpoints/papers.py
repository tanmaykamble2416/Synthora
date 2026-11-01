import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response, status

from app.api.deps import CurrentUser, DbSession, SettingsDep, StorageDep
from app.api.uploads import UPLOAD_OPENAPI, single_file_upload
from app.schemas.common import Page
from app.schemas.error import ErrorResponse
from app.schemas.paper import PaperRead, PaperUpdate
from app.services.paper_service import PaperService

router = APIRouter(
    tags=["papers"],
    responses={401: {"model": ErrorResponse, "description": "Not authenticated"}},
)

NOT_FOUND = {404: {"model": ErrorResponse, "description": "Not found (or owned by another user)"}}
UPLOAD_ERRORS = {
    **NOT_FOUND,
    400: {"model": ErrorResponse, "description": "Not a valid PDF, empty file or malformed upload"},
    409: {"model": ErrorResponse, "description": "The same PDF is already in this workspace"},
    413: {"model": ErrorResponse, "description": "Upload larger than MAX_UPLOAD_SIZE_MB"},
    415: {"model": ErrorResponse, "description": "Not a .pdf file / not application/pdf"},
}


@router.post(
    "/workspaces/{workspace_id}/papers",
    response_model=PaperRead,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a PDF into one of my workspaces (multipart field 'file')",
    responses=UPLOAD_ERRORS,
    openapi_extra=UPLOAD_OPENAPI,
)
async def upload_paper(
    workspace_id: uuid.UUID,
    request: Request,
    response: Response,
    user: CurrentUser,
    session: DbSession,
    storage: StorageDep,
    settings: SettingsDep,
) -> PaperRead:
    # No File() parameter on purpose: FastAPI would read the whole body before the
    # auth/ownership dependencies run. Here the body is read only after both pass.
    service = PaperService(session, storage)
    await service.require_owned_workspace(user.id, workspace_id)
    async with single_file_upload(request, settings.max_upload_bytes) as upload:
        paper = await service.upload_pdf(user.id, workspace_id, upload, max_bytes=settings.max_upload_bytes)
    response.headers["Location"] = f"{settings.api_v1_prefix}/papers/{paper.id}"
    return PaperRead.model_validate(paper)


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
    storage: StorageDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> Page[PaperRead]:
    return await PaperService(session, storage).list_for_workspace(
        user.id, workspace_id, limit=limit, offset=offset
    )


@router.get("/papers/{paper_id}", response_model=PaperRead, summary="Get one of my papers", responses=NOT_FOUND)
async def get_paper(paper_id: uuid.UUID, user: CurrentUser, session: DbSession, storage: StorageDep) -> PaperRead:
    paper = await PaperService(session, storage).get(user.id, paper_id)
    return PaperRead.model_validate(paper)


@router.patch(
    "/papers/{paper_id}",
    response_model=PaperRead,
    summary="Update bibliographic metadata (title, abstract, authors, publication_year, doi)",
    responses=NOT_FOUND,
)
async def update_paper(
    paper_id: uuid.UUID, data: PaperUpdate, user: CurrentUser, session: DbSession, storage: StorageDep
) -> PaperRead:
    paper = await PaperService(session, storage).update_metadata(user.id, paper_id, data)
    return PaperRead.model_validate(paper)


@router.delete(
    "/papers/{paper_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a paper and its stored PDF",
    responses=NOT_FOUND,
)
async def delete_paper(paper_id: uuid.UUID, user: CurrentUser, session: DbSession, storage: StorageDep) -> Response:
    await PaperService(session, storage).delete(user.id, paper_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
