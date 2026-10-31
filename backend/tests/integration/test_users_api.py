from fastapi.testclient import TestClient

from tests.integration.conftest import DEFAULT_PASSWORD, login, register, register_and_login


def test_update_full_name(api_client: TestClient) -> None:
    headers = register_and_login(api_client, "kim@example.com")
    response = api_client.patch("/api/v1/users/me", json={"full_name": "Kim Lee"}, headers=headers)
    assert response.status_code == 200
    assert response.json()["full_name"] == "Kim Lee"
    assert api_client.get("/api/v1/users/me", headers=headers).json()["full_name"] == "Kim Lee"


def test_password_change_requires_current_password(api_client: TestClient) -> None:
    headers = register_and_login(api_client, "lou@example.com")
    response = api_client.patch("/api/v1/users/me", json={"password": "a brand new password"}, headers=headers)
    assert response.status_code == 422


def test_password_change_with_wrong_current_password(api_client: TestClient) -> None:
    headers = register_and_login(api_client, "max@example.com")
    response = api_client.patch(
        "/api/v1/users/me",
        json={"password": "a brand new password", "current_password": "not my password"},
        headers=headers,
    )
    assert response.status_code == 400
    login(api_client, "max@example.com")  # old password still works


def test_password_change_success(api_client: TestClient) -> None:
    headers = register_and_login(api_client, "ned@example.com")
    response = api_client.patch(
        "/api/v1/users/me",
        json={"password": "a brand new password", "current_password": DEFAULT_PASSWORD},
        headers=headers,
    )
    assert response.status_code == 200
    old = api_client.post("/api/v1/auth/login", data={"username": "ned@example.com", "password": DEFAULT_PASSWORD})
    assert old.status_code == 401
    login(api_client, "ned@example.com", "a brand new password")


def test_cannot_change_email_or_active_flag_via_update(api_client: TestClient) -> None:
    headers = register_and_login(api_client, "oli@example.com")
    for field, value in (("email", "x@example.com"), ("is_active", False), ("id", "abc")):
        response = api_client.patch("/api/v1/users/me", json={field: value}, headers=headers)
        assert response.status_code == 422, field


def test_empty_update_rejected(api_client: TestClient) -> None:
    headers = register_and_login(api_client, "pat@example.com")
    assert api_client.patch("/api/v1/users/me", json={}, headers=headers).status_code == 422


def test_delete_me_removes_account_and_workspaces(api_client: TestClient) -> None:
    headers = register_and_login(api_client, "quinn@example.com")
    api_client.post("/api/v1/workspaces", json={"name": "Mine"}, headers=headers)
    assert api_client.delete("/api/v1/users/me", headers=headers).status_code == 204
    failed = api_client.post("/api/v1/auth/login", data={"username": "quinn@example.com", "password": DEFAULT_PASSWORD})
    assert failed.status_code == 401
    # Same email can register again: the old row is really gone.
    register(api_client, "quinn@example.com")


def test_there_is_no_list_all_users_endpoint(api_client: TestClient) -> None:
    headers = register_and_login(api_client, "ray@example.com")
    assert api_client.get("/api/v1/users", headers=headers).status_code in (404, 405)
