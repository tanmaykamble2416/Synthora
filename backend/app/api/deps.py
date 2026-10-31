"""Shared FastAPI dependencies: DB session, settings and the authenticated user."""

from typing import Annotated

from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.exceptions import AuthenticationError
from app.core.security import INVALID_TOKEN_MESSAGE, decode_access_token
from app.db.session import get_db_session
from app.models.user import User
from app.repositories.user_repository import UserRepository

# auto_error=False: we raise our own AuthenticationError so the error shape is consistent.
# tokenUrl is a literal (not read from settings) so importing this module never reads .env.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)

DbSession = Annotated[AsyncSession, Depends(get_db_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


async def get_current_user(
    token: Annotated[str | None, Depends(oauth2_scheme)],
    session: DbSession,
    settings: SettingsDep,
) -> User:
    if not token:
        raise AuthenticationError("Not authenticated.")
    user_id = decode_access_token(token, settings)
    # Re-load on every request: deleted or deactivated users lose access immediately.
    user = await UserRepository(session).get_by_id(user_id)
    if user is None or not user.is_active:
        raise AuthenticationError(INVALID_TOKEN_MESSAGE)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
