import uuid

import pytest
from fastapi.testclient import TestClient

from tests.integration.conftest import register_and_login

WS = "/api/v1/workspaces"


@pytest.fixture
def alice(api_client: TestClient) -> dict[str, str]:
    return register_and_login(api_client, "alice@example.com")


@pytest.fixture
def bob(api_client: TestClient) -> dict[str, str]:
    return register_and_login(api_client, "bob@example.com")


def _create(client: TestClient, headers: dict, name: str = "NLP Survey", **extra) -> dict:
    response = client.post(WS, json={"name": name, **extra}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_crud_happy_path(api_client: TestClient, alice: dict) -> None:
    created = _create(api_client, alice, "  NLP Survey  ", description="Transformers")
    assert created["name"] == "NLP Survey"  # whitespace stripped
    ws_id = created["id"]

    assert api_client.get(f"{WS}/{ws_id}", headers=alice).json()["description"] == "Transformers"

    updated = api_client.patch(f"{WS}/{ws_id}", json={"description": None}, headers=alice)
    assert updated.status_code == 200
    assert updated.json()["description"] is None
    assert updated.json()["name"] == "NLP Survey"
    assert updated.json()["updated_at"] >= created["updated_at"]

    assert api_client.delete(f"{WS}/{ws_id}", headers=alice).status_code == 204
    assert api_client.get(f"{WS}/{ws_id}", headers=alice).status_code == 404


def test_list_is_scoped_paginated_and_newest_first(api_client: TestClient, alice: dict, bob: dict) -> None:
    for i in range(3):
        _create(api_client, alice, f"A{i}")
    _create(api_client, bob, "B0")

    page = api_client.get(WS, params={"limit": 2, "offset": 0}, headers=alice).json()
    assert page["total"] == 3
    assert [w["name"] for w in page["items"]] == ["A2", "A1"]
    page2 = api_client.get(WS, params={"limit": 2, "offset": 2}, headers=alice).json()
    assert [w["name"] for w in page2["items"]] == ["A0"]
    assert api_client.get(WS, headers=bob).json()["total"] == 1


def test_duplicate_name_conflicts_case_insensitively(api_client: TestClient, alice: dict, bob: dict) -> None:
    _create(api_client, alice, "Vision")
    duplicate = api_client.post(WS, json={"name": "vision"}, headers=alice)
    assert duplicate.status_code == 409
    _create(api_client, bob, "Vision")  # different owner: allowed


def test_rename_to_existing_name_conflicts(api_client: TestClient, alice: dict) -> None:
    _create(api_client, alice, "One")
    two = _create(api_client, alice, "Two")
    response = api_client.patch(f"{WS}/{two['id']}", json={"name": "ONE"}, headers=alice)
    assert response.status_code == 409


def test_other_users_workspace_is_invisible_and_untouchable(
    api_client: TestClient, alice: dict, bob: dict
) -> None:
    ws = _create(api_client, alice, "Private")
    url = f"{WS}/{ws['id']}"

    for response in (
        api_client.get(url, headers=bob),
        api_client.patch(url, json={"name": "Hijacked"}, headers=bob),
        api_client.delete(url, headers=bob),
    ):
        assert response.status_code == 404  # not 403: existence is not revealed
        assert response.json()["error"]["code"] == "NOT_FOUND"

    still_there = api_client.get(url, headers=alice).json()
    assert still_there["name"] == "Private"


def test_owner_id_cannot_be_supplied(api_client: TestClient, alice: dict) -> None:
    response = api_client.post(WS, json={"name": "X", "owner_id": str(uuid.uuid4())}, headers=alice)
    assert response.status_code == 422
    ws = _create(api_client, alice, "Y")
    response = api_client.patch(f"{WS}/{ws['id']}", json={"owner_id": str(uuid.uuid4())}, headers=alice)
    assert response.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [{"name": ""}, {"name": "   "}, {"name": "x" * 201}, {"description": "d"}, {"name": "ok", "description": "x" * 5001}],
)
def test_create_validation(api_client: TestClient, alice: dict, payload: dict) -> None:
    assert api_client.post(WS, json=payload, headers=alice).status_code == 422


@pytest.mark.parametrize("payload", [{}, {"name": None}])
def test_update_validation(api_client: TestClient, alice: dict, payload: dict) -> None:
    ws = _create(api_client, alice)
    assert api_client.patch(f"{WS}/{ws['id']}", json=payload, headers=alice).status_code == 422


def test_invalid_ids_and_pagination(api_client: TestClient, alice: dict) -> None:
    assert api_client.get(f"{WS}/not-a-uuid", headers=alice).status_code == 422
    assert api_client.get(f"{WS}/{uuid.uuid4()}", headers=alice).status_code == 404
    assert api_client.get(WS, params={"limit": 101}, headers=alice).status_code == 422
    assert api_client.get(WS, params={"offset": -1}, headers=alice).status_code == 422


def test_workspace_routes_require_auth(api_client: TestClient) -> None:
    assert api_client.get(WS).status_code == 401
    assert api_client.post(WS, json={"name": "x"}).status_code == 401
