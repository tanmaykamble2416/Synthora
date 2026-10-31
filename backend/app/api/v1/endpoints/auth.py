from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import DbSession, SettingsDep
from app.schemas.auth import TokenResponse
from app.schemas.error import ErrorResponse
from app.schemas.user import UserCreate, UserRead
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
    responses={409: {"model": ErrorResponse, "description": "Email already registered"}},
)
async def register(data: UserCreate, session: DbSession, settings: SettingsDep) -> UserRead:
    user = await AuthService(session, settings).register(data)
    return UserRead.model_validate(user)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Exchange email + password for an access token (OAuth2 password form; 'username' = email)",
    responses={401: {"model": ErrorResponse, "description": "Incorrect email or password"}},
)
async def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()], session: DbSession, settings: SettingsDep
) -> TokenResponse:
    return await AuthService(session, settings).login(form.username, form.password)
