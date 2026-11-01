"""Shared fixtures.

Unit tests use `unit_env`, which sets explicit environment variables so the
developer's real .env never leaks into them. Real env vars override .env in
pydantic-settings, so these values always win.

Everything under tests/integration/ is automatically marked `integration` and
skipped unless RUN_DB_TESTS=1. Those tests only ever use the guarded test database
(see tests/integration/conftest.py).
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import get_settings

INTEGRATION_DIR = Path(__file__).parent / "integration"

# Test-only signing key (>= 32 chars). Never used outside tests.
TEST_JWT_SECRET = "unit-test-jwt-secret-key-0123456789-abcdefghij"

UNIT_ENV = {
    "ENVIRONMENT": "test",
    "POSTGRES_HOST": "127.0.0.1",
    # Port 1 is never a Postgres server -> connection refused, quickly.
    "POSTGRES_PORT": "1",
    "POSTGRES_USER": "test_user",
    "POSTGRES_PASSWORD": "test_password",
    "POSTGRES_DB": "test_db",
    "DB_CONNECT_TIMEOUT": "2",
    "JWT_SECRET_KEY": TEST_JWT_SECRET,
}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    run_db_tests = os.getenv("RUN_DB_TESTS") == "1"
    skip = pytest.mark.skip(reason="set RUN_DB_TESTS=1 to run integration tests")
    for item in items:
        if INTEGRATION_DIR in Path(item.path).parents:
            item.add_marker(pytest.mark.integration)
            if not run_db_tests:
                item.add_marker(skip)


@pytest.fixture
def unit_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    for key, value in UNIT_ENV.items():
        monkeypatch.setenv(key, value)
    # Never let a test touch the real storage directory.
    monkeypatch.setenv("PAPER_STORAGE_DIR", str(tmp_path / "paper-storage"))
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
