import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AuthenticationError, ConflictError
from app.core.security import (
    burn_password_verification_time,
    create_access_token,
    hash_password,
    password_needs_rehash,
    verify_password,
)
from app.db.errors import is_unique_violation
from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.schemas.auth import TokenResponse
from app.schemas.user import UserCreate

logger = logging.getLogger(__name__)

LOGIN_FAILED_MESSAGE = "Incorrect email or password."


class AuthService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._users = UserRepository(session)

    async def register(self, data: UserCreate) -> User:
        try:
            user = await self._users.create(
                email=data.email, hashed_password=hash_password(data.password), full_name=data.full_name
            )
            await self._session.commit()
        except IntegrityError as exc:
            # Relies on the UNIQUE constraint (no check-then-insert race).
            await self._session.rollback()
            if is_unique_violation(exc, "uq_users_email"):
                raise ConflictError("An account with this email already exists.") from exc
            raise
        logger.info("Registered user %s", user.id)
        return user

    async def login(self, email: str, password: str) -> TokenResponse:
        user = await self._users.get_by_email(email.strip().lower())
        if user is None:
            burn_password_verification_time(password)
            raise AuthenticationError(LOGIN_FAILED_MESSAGE)
        if not verify_password(user.hashed_password, password) or not user.is_active:
            raise AuthenticationError(LOGIN_FAILED_MESSAGE)

        if password_needs_rehash(user.hashed_password):
            await self._users.update(user.id, {"hashed_password": hash_password(password)})
            await self._session.commit()

        token, expires_in = create_access_token(user.id, self._settings)
        logger.info("User %s logged in", user.id)  # never log the token
        return TokenResponse(access_token=token, expires_in=expires_in)
