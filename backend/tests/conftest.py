"""Shared fixtures.

Unit tests use `unit_env`, which sets explicit environment variables so the
developer's real .env never leaks into them. Real env vars override .env in
pydantic-settings, so these values always win.
"""

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import get_settings

UNIT_ENV = {
    "ENVIRONMENT": "test",
    "POSTGRES_HOST": "127.0.0.1",
    # Port 1 is never a Postgres server -> connection refused, quickly.
    "POSTGRES_PORT": "1",
    "POSTGRES_USER": "test_user",
    "POSTGRES_PASSWORD": "test_password",
    "POSTGRES_DB": "test_db",
    "DB_CONNECT_TIMEOUT": "2",
}


@pytest.fixture
def unit_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key, value in UNIT_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def app(unit_env: None) -> FastAPI:
    from app.main import create_app

    return create_app()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    # `with` runs the lifespan (engine creation + disposal).
    # raise_server_exceptions=False lets us assert on the 500 JSON body.
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
