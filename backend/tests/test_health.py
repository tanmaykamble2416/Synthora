from typing import Annotated

import pytest
from fastapi import FastAPI, Query
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.v1.endpoints import health as health_endpoints
from app.core.exceptions import ExtensionMissingError
from app.schemas.health import DatabaseHealthResponse


def test_liveness_returns_ok_without_database(client: TestClient) -> None:
    # The unit env points at port 1 (no DB) — liveness must still be 200.
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["environment"] == "test"


def test_db_health_returns_503_when_database_unreachable(client: TestClient) -> None:
    response = client.get("/api/v1/health/db")
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "DATABASE_UNAVAILABLE"
    # No connection details or stack traces leak to the client.
    assert "test_password" not in response.text
    assert "Traceback" not in response.text


def test_db_health_success_shape(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_check(engine: AsyncEngine) -> DatabaseHealthResponse:
        return DatabaseHealthResponse(
            status="ok",
            database="test_db",
            server_version="16.0",
            pgvector_version="0.8.0",
            latency_ms=1.0,
        )

    monkeypatch.setattr(health_endpoints, "check_database", fake_check)
    response = client.get("/api/v1/health/db")
    assert response.status_code == 200
    assert response.json()["pgvector_version"] == "0.8.0"


def test_db_health_503_when_pgvector_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_check(engine: AsyncEngine) -> DatabaseHealthResponse:
        raise ExtensionMissingError("PostgreSQL extension 'vector' is not installed.")

    monkeypatch.setattr(health_endpoints, "check_database", fake_check)
    response = client.get("/api/v1/health/db")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DATABASE_EXTENSION_MISSING"


def test_unknown_route_uses_consistent_error_shape(client: TestClient) -> None:
    response = client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "HTTP_404", "message": "Not Found", "details": None}
    }


def test_validation_errors_use_consistent_error_shape(app: FastAPI) -> None:
    @app.get("/_test/validate")
    async def validate(limit: Annotated[int, Query(ge=1, le=10)]) -> dict[str, int]:
        return {"limit": limit}

    with TestClient(app) as test_client:
        response = test_client.get("/_test/validate", params={"limit": 99})
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"][0]["loc"] == ["query", "limit"]


def test_unexpected_errors_return_generic_500(app: FastAPI) -> None:
    @app.get("/_test/boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail")

    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.get("/_test/boom")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "secret internal detail" not in response.text


def test_openapi_documents_health_routes(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/health" in paths
    assert "/api/v1/health/db" in paths
