"""Registration, login and token handling through the real API + test DB."""

import logging
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_JWT_SECRET
from tests.integration.conftest import DEFAULT_PASSWORD, login, register


def test_register_returns_user_without_password_fields(api_client: TestClient) -> None:
    body = register(api_client, "Alice@Example.COM")
    assert body["email"] == "alice@example.com"  # normalised
    assert uuid.UUID(body["id"])
    assert body["is_active"] is True
    assert "password" not in str(body).lower()
    assert "hash" not in str(body).lower()


def test_duplicate_email_is_rejected_case_insensitively(api_client: TestClient) -> None:
    register(api_client, "bob@example.com")
    response = api_client.post(
        "/api/v1/auth/register", json={"email": "BOB@example.com", "password": DEFAULT_PASSWORD}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "not-an-email", "password": DEFAULT_PASSWORD},
        {"email": "c@example.com", "password": "short"},
        {"email": "c@example.com", "password": "x" * 129},
        {"email": "c@example.com"},
        {"email": "c@example.com", "password": DEFAULT_PASSWORD, "is_active": False},  # unknown field
        {"email": "c@example.com", "password": DEFAULT_PASSWORD, "id": str(uuid.uuid4())},
    ],
)
def test_register_validation(api_client: TestClient, payload: dict) -> None:
    response = api_client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_rejected_password_is_not_echoed(api_client: TestClient) -> None:
    response = api_client.post("/api/v1/auth/register", json={"email": "d@example.com", "password": "tiny7"})
    assert response.status_code == 422
    assert "tiny7" not in response.text


def test_login_returns_bearer_token(api_client: TestClient) -> None:
    user = register(api_client, "erin@example.com")
    response = api_client.post(
        "/api/v1/auth/login", data={"username": "ERIN@example.com", "password": DEFAULT_PASSWORD}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 30 * 60
    claims = jwt.decode(
        body["access_token"], TEST_JWT_SECRET, algorithms=["HS256"], audience="synthora-clients"
    )
    assert claims["sub"] == user["id"]
    assert claims["type"] == "access"
    assert claims["iss"] == "synthora-api"


def test_wrong_password_and_unknown_email_look_identical(api_client: TestClient) -> None:
    register(api_client, "frank@example.com")
    wrong_pw = api_client.post("/api/v1/auth/login", data={"username": "frank@example.com", "password": "nope-nope-nope"})
    unknown = api_client.post("/api/v1/auth/login", data={"username": "ghost@example.com", "password": "nope-nope-nope"})
    assert wrong_pw.status_code == unknown.status_code == 401
    assert wrong_pw.json() == unknown.json()
    assert wrong_pw.headers["www-authenticate"] == "Bearer"


def test_me_requires_token(api_client: TestClient) -> None:
    response = api_client.get("/api/v1/users/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_FAILED"
    assert response.headers["www-authenticate"] == "Bearer"


def test_me_with_valid_token(api_client: TestClient) -> None:
    user = register(api_client, "gina@example.com")
    response = api_client.get("/api/v1/users/me", headers=login(api_client, "gina@example.com"))
    assert response.status_code == 200
    assert response.json()["id"] == user["id"]


def _forge(sub: str, **overrides) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": sub, "type": "access", "iss": "synthora-api", "aud": "synthora-clients",
        "iat": now, "nbf": now, "exp": now + timedelta(minutes=5),
    }
    claims.update(overrides)
    return jwt.encode(claims, TEST_JWT_SECRET, algorithm="HS256")


@pytest.mark.parametrize(
    "make_token",
    [
        pytest.param(lambda uid: "garbage", id="garbage"),
        pytest.param(lambda uid: _forge(uid, exp=datetime.now(UTC) - timedelta(seconds=5)), id="expired"),
        pytest.param(lambda uid: _forge(uid, aud="evil"), id="wrong-audience"),
        pytest.param(lambda uid: _forge(uid, iss="evil"), id="wrong-issuer"),
        pytest.param(lambda uid: _forge(uid, type="refresh"), id="wrong-type"),
        pytest.param(lambda uid: _forge("not-a-uuid"), id="non-uuid-sub"),
        pytest.param(lambda uid: _forge(str(uuid.uuid4())), id="unknown-user"),
        pytest.param(
            lambda uid: jwt.encode({"sub": uid}, "x" * 40, algorithm="HS256"), id="wrong-key-missing-claims"
        ),
    ],
)
def test_invalid_tokens_are_rejected(api_client: TestClient, make_token) -> None:
    user = register(api_client, "hank@example.com")
    response = api_client.get(
        "/api/v1/users/me", headers={"Authorization": f"Bearer {make_token(user['id'])}"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_FAILED"


def test_token_of_deleted_user_stops_working(api_client: TestClient) -> None:
    register(api_client, "ivy@example.com")
    headers = login(api_client, "ivy@example.com")
    assert api_client.delete("/api/v1/users/me", headers=headers).status_code == 204
    assert api_client.get("/api/v1/users/me", headers=headers).status_code == 401


def test_secrets_and_tokens_never_logged(api_client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    register(api_client, "jo@example.com")
    response = api_client.post("/api/v1/auth/login", data={"username": "jo@example.com", "password": DEFAULT_PASSWORD})
    token = response.json()["access_token"]
    api_client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {token}"})
    api_client.post("/api/v1/auth/login", data={"username": "jo@example.com", "password": "wrong-password-1"})

    logs = caplog.text
    assert "logged in" in logs  # proves log capture is actually working
    assert TEST_JWT_SECRET not in logs
    assert token not in logs
    assert DEFAULT_PASSWORD not in logs
    assert "wrong-password-1" not in logs
    assert "$argon2" not in logs
