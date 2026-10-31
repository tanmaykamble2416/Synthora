"""Async Alembic environment.

URL resolution:
  * Tests pass an explicit URL via config.attributes["database_url"] (the guarded test DB).
  * Otherwise the URL comes from app Settings, i.e. POSTGRES_* in the root .env.
The engine is created from a sqlalchemy URL object, so passwords containing '%', '@'
or ':' are never parsed through alembic.ini's interpolation.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy.engine import URL, Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401  (registers all tables on Base.metadata)
from alembic import context
from app.core.config import get_settings
from app.db.base import Base

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    # disable_existing_loggers=False keeps the app's (and pytest's) loggers working.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> URL:
    explicit = config.attributes.get("database_url")
    if explicit is not None:
        return explicit
    return get_settings().database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout (alembic upgrade head --sql). No connection, no password needed."""
    context.configure(
        url=_database_url().render_as_string(hide_password=True),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_sync_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_database_url(), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_sync_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
