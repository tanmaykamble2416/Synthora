"""Data access for users. Repositories flush but never commit; services own the transaction."""

import uuid
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return await self._session.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        result = await self._session.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    async def create(self, *, email: str, hashed_password: str, full_name: str | None) -> User:
        user = User(email=email, hashed_password=hashed_password, full_name=full_name)
        self._session.add(user)
        await self._session.flush()
        return user

    async def update(self, user_id: uuid.UUID, values: dict[str, Any]) -> User | None:
        stmt = update(User).where(User.id == user_id).values(**values).returning(User)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def delete(self, user_id: uuid.UUID) -> bool:
        # Workspaces -> papers -> sections/chunks are removed by ON DELETE CASCADE.
        result = await self._session.execute(delete(User).where(User.id == user_id).returning(User.id))
        return result.scalar_one_or_none() is not None
