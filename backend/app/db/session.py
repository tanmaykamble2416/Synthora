"""Async SQLAlchemy engine/session factories and FastAPI dependencies.

The engine is created once in the app lifespan (app/main.py) and stored on
app.state, so there is no import-time database connection and tests can
swap it out.
"""

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings


def create_db_engine(settings: Settings) -> AsyncEngine:
    """Create the connection pool. No connection is opened until first use."""
    return create_async_engine(
        settings.database_url,
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_pre_ping=True,  # drop dead connections (e.g. after a DB restart)
        connect_args={"timeout": settings.db_connect_timeout},  # asyncpg connect timeout (s)
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: objects stay readable after commit, which matters
    # in async code where lazy re-loading would need another awaited query.
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


def get_engine(request: Request) -> AsyncEngine:
    return request.app.state.engine


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request. Services commit explicitly; any exception rolls back."""
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
