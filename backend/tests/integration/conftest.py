"""Integration fixtures. These tests ONLY touch TEST_POSTGRES_DB (default researchmind_test).

Safety layers:
  1. The test DB name is validated by assert_safe_test_database (must end in _test,
     differ from POSTGRES_DB, be a plain identifier) before any connection is made.
  2. Every engine/URL used here is built with database_url_for(test_db).
  3. The test DB is created if missing; no database is ever dropped. Data is reset
     with TRUNCATE on the test DB only.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from app.core.config import ENV_FILE, Settings, get_settings
from tests.conftest import TEST_JWT_SECRET
from tests.db_safety import assert_safe_test_database

BACKEND_DIR = Path(__file__).resolve().parents[2]
APP_TABLES = ("paper_chunks", "paper_sections", "papers", "workspaces", "users")


class _TestDatabaseSettings(BaseSettings):
    """Tests-only setting, read from the same root .env (or the environment)."""

    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")
    test_postgres_db: str = "researchmind_test"


@pytest.fixture(scope="session")
def base_settings() -> Settings:
    # Real Postgres host/user/password from .env; test-only JWT secret (init kwargs win).
    return Settings(jwt_secret_key=TEST_JWT_SECRET, environment="test")


@pytest.fixture(scope="session")
def test_db_name(base_settings: Settings) -> str:
    name = _TestDatabaseSettings().test_postgres_db
    assert_safe_test_database(name, base_settings.postgres_db)
    return name


@pytest.fixture(scope="session")
def test_db_url(base_settings: Settings, test_db_name: str) -> URL:
    return base_settings.database_url_for(test_db_name)


@pytest.fixture(scope="session")
def alembic_config(test_db_url: URL) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.attributes["database_url"] = test_db_url
    cfg.attributes["configure_logger"] = False
    return cfg


async def _ensure_database_exists(settings: Settings, name: str) -> None:
    # Connects to the maintenance DB only to CREATE the test DB when it is missing.
    engine = create_async_engine(
        settings.database_url_for("postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    try:
        async with engine.connect() as conn:
            exists = (
                await conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name})
            ).scalar_one_or_none()
            if not exists:
                # Identifiers cannot be bound parameters; `name` was validated by the guard.
                await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def migrated_test_db(base_settings: Settings, test_db_name: str, alembic_config: Config) -> str:
    asyncio.run(_ensure_database_exists(base_settings, test_db_name))
    command.upgrade(alembic_config, "head")
    return test_db_name


async def _truncate(url: URL) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            current = (await conn.execute(text("SELECT current_database()"))).scalar_one()
            assert current.endswith("_test"), f"refusing to TRUNCATE {current!r}"
            await conn.execute(text(f"TRUNCATE {', '.join(APP_TABLES)} RESTART IDENTITY CASCADE"))
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def clean_tables(migrated_test_db: str, test_db_url: URL) -> None:
    # Reset before each test (so a failed test cannot leak data into the next one).
    asyncio.run(_truncate(test_db_url))


@pytest.fixture
async def db_session(test_db_url: URL) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(test_db_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


@pytest.fixture
def api_client(monkeypatch: pytest.MonkeyPatch, test_db_name: str) -> Iterator[TestClient]:
    """The real app, pointed at the test DB via environment overrides."""
    monkeypatch.setenv("POSTGRES_DB", test_db_name)
    monkeypatch.setenv("JWT_SECRET_KEY", TEST_JWT_SECRET)
    monkeypatch.setenv("ENVIRONMENT", "test")
    get_settings.cache_clear()
    assert get_settings().postgres_db == test_db_name

    from app.main import create_app

    with TestClient(create_app()) as client:
        yield client
    get_settings.cache_clear()


DEFAULT_PASSWORD = "correct horse battery staple"


def register(client: TestClient, email: str, password: str = DEFAULT_PASSWORD) -> dict:
    response = client.post("/api/v1/auth/register", json={"email": email, "password": password})
    assert response.status_code == 201, response.text
    return response.json()


def login(client: TestClient, email: str, password: str = DEFAULT_PASSWORD) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", data={"username": email, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def register_and_login(client: TestClient, email: str) -> dict[str, str]:
    register(client, email)
    return login(client, email)
