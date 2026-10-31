import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BadRequestError, NotFoundError
from app.core.security import hash_password, verify_password
from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.schemas.user import UserUpdate

logger = logging.getLogger(__name__)


class UserService:
    """Self-service only: a user can read, update and delete their own account.
    There is intentionally no 'list all users' operation."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._users = UserRepository(session)

    async def update_me(self, user: User, data: UserUpdate) -> User:
        values: dict[str, object] = {}
        if "full_name" in data.model_fields_set:
            values["full_name"] = data.full_name
        if data.password is not None:
            if not verify_password(user.hashed_password, data.current_password or ""):
                raise BadRequestError("Current password is incorrect.")
            values["hashed_password"] = hash_password(data.password)

        updated = await self._users.update(user.id, values)
        if updated is None:
            raise NotFoundError("User not found.")
        await self._session.commit()
        return updated

    async def delete_me(self, user: User) -> None:
        if not await self._users.delete(user.id):
            raise NotFoundError("User not found.")
        await self._session.commit()
        logger.info("Deleted user %s and all owned data", user.id)
