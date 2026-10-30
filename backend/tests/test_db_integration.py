"""Integration test against a REAL PostgreSQL (the docker-compose one).

Skipped by default. Run with:
    RUN_DB_TESTS=1 pytest -m integration          (Linux/macOS)
    $env:RUN_DB_TESTS=1; pytest -m integration    (Windows PowerShell)

Uses the real settings from the project-root .env.
"""

import os

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="set RUN_DB_TESTS=1 to run"),
]


def test_real_database_health_with_pgvector() -> None:
    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app()) as client:
        response = client.get("/api/v1/health/db")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == get_settings().postgres_db
    assert body["server_version"].startswith("16")
    assert body["pgvector_version"]
