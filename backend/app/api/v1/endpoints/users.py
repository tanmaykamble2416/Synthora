from fastapi import APIRouter, Response, status

from app.api.deps import CurrentUser, DbSession
from app.schemas.error import ErrorResponse
from app.schemas.user import UserRead, UserUpdate
from app.services.user_service import UserService

router = APIRouter(
    prefix="/users",
    tags=["users"],
    responses={401: {"model": ErrorResponse, "description": "Not authenticated"}},
)


@router.get("/me", response_model=UserRead, summary="Get my account")
async def read_me(user: CurrentUser) -> UserRead:
    return UserRead.model_validate(user)


@router.patch("/me", response_model=UserRead, summary="Update my name and/or password")
async def update_me(data: UserUpdate, user: CurrentUser, session: DbSession) -> UserRead:
    updated = await UserService(session).update_me(user, data)
    return UserRead.model_validate(updated)


@router.delete(
    "/me",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete my account and all my workspaces/papers",
)
async def delete_me(user: CurrentUser, session: DbSession) -> Response:
    await UserService(session).delete_me(user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
