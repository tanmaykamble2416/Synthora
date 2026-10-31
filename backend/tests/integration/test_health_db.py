"""Day 1 readiness check, now run against the guarded test database."""

from fastapi.testclient import TestClient


def test_real_database_health_with_pgvector(api_client: TestClient, test_db_name: str) -> None:
    response = api_client.get("/api/v1/health/db")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == test_db_name
    assert body["server_version"].startswith("16")
    assert body["pgvector_version"]
