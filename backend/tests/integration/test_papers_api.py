"""Paper management API (Day 3): list, get, update metadata, delete.

There is no upload endpoint until Day 4, so papers are seeded directly into the
guarded test database. Explicit created_at values make ordering tests deterministic.
"""

import asyncio
import hashlib
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.models import Paper, User, Workspace
from app.repositories.paper_repository import PaperRepository
from tests.integration.conftest import register_and_login

BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)
INTERNAL_FIELDS = {
    "id": str(uuid.uuid4()),
    "workspace_id": str(uuid.uuid4()),
    "storage_key": "../../etc/passwd",
    "sha256": "0" * 64,
    "file_size_bytes": 1,
    "page_count": 3,
    "status": "processed",
    "error_message": "x",
    "processed_at": "2026-01-01T00:00:00Z",
    "original_filename": "evil.pdf",
    "owner_id": str(uuid.uuid4()),
    "created_at": "2020-01-01T00:00:00Z",
}


async def _run(url: URL, sql: str, params: dict[str, Any]) -> list[Any]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            current = (await conn.execute(text("SELECT current_database()"))).scalar_one()
            assert current.endswith("_test"), f"refusing to write to {current!r}"
            result = await conn.execute(text(sql), params)
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


SeedPaper = Callable[..., str]


@pytest.fixture
def seed_paper(test_db_url: URL) -> SeedPaper:
    """Insert a paper row into the test DB and return its id."""

    def _seed(workspace_id: str, *, minutes: int = 0, **columns: Any) -> str:
        paper_id = str(uuid.uuid4())
        values = {
            "id": paper_id,
            "workspace_id": workspace_id,
            "original_filename": f"{paper_id[:8]}.pdf",
            "storage_key": f"papers/{paper_id}.pdf",
            "sha256": hashlib.sha256(paper_id.encode()).hexdigest(),
            "file_size_bytes": 2048,
            "created_at": BASE_TIME + timedelta(minutes=minutes),
            **columns,
        }
        cols = ", ".join(values)
        placeholders = ", ".join(f":{c}" for c in values)
        asyncio.run(_run(test_db_url, f"INSERT INTO papers ({cols}) VALUES ({placeholders})", values))
        return paper_id

    return _seed


@pytest.fixture
def query(test_db_url: URL) -> Callable[[str, dict[str, Any]], list[Any]]:
    return lambda sql, params: asyncio.run(_run(test_db_url, sql, params))


@pytest.fixture
def alice(api_client: TestClient) -> dict[str, str]:
    return register_and_login(api_client, "alice@example.com")


@pytest.fixture
def bob(api_client: TestClient) -> dict[str, str]:
    return register_and_login(api_client, "bob@example.com")


def _workspace(client: TestClient, headers: dict[str, str], name: str = "Survey") -> str:
    response = client.post("/api/v1/workspaces", json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _papers_url(workspace_id: str) -> str:
    return f"/api/v1/workspaces/{workspace_id}/papers"


def _paper_url(paper_id: str) -> str:
    return f"/api/v1/papers/{paper_id}"


# --- 1-4. Listing, pagination, ordering, total ----------------------------------------


def test_list_papers_in_owned_workspace(api_client: TestClient, alice: dict, seed_paper: SeedPaper) -> None:
    ws = _workspace(api_client, alice)
    paper_id = seed_paper(ws, title="Attention Is All You Need")

    response = api_client.get(_papers_url(ws), headers=alice)

    assert response.status_code == 200
    body = response.json()
    assert body == {"items": body["items"], "total": 1, "limit": 20, "offset": 0}
    assert body["items"][0]["id"] == paper_id
    assert body["items"][0]["title"] == "Attention Is All You Need"
    assert body["items"][0]["status"] == "uploaded"


def test_empty_workspace_lists_nothing(api_client: TestClient, alice: dict) -> None:
    ws = _workspace(api_client, alice)
    assert api_client.get(_papers_url(ws), headers=alice).json() == {
        "items": [], "total": 0, "limit": 20, "offset": 0
    }


def test_pagination_sorting_and_total(api_client: TestClient, alice: dict, seed_paper: SeedPaper) -> None:
    ws = _workspace(api_client, alice)
    ids_oldest_first = [seed_paper(ws, minutes=i) for i in range(5)]
    newest_first = list(reversed(ids_oldest_first))

    page1 = api_client.get(_papers_url(ws), params={"limit": 2, "offset": 0}, headers=alice).json()
    page2 = api_client.get(_papers_url(ws), params={"limit": 2, "offset": 2}, headers=alice).json()
    page3 = api_client.get(_papers_url(ws), params={"limit": 2, "offset": 4}, headers=alice).json()
    past_end = api_client.get(_papers_url(ws), params={"limit": 2, "offset": 10}, headers=alice).json()

    assert [p["id"] for p in page1["items"]] == newest_first[0:2]
    assert [p["id"] for p in page2["items"]] == newest_first[2:4]
    assert [p["id"] for p in page3["items"]] == newest_first[4:5]
    assert past_end["items"] == []
    assert {page1["total"], page2["total"], page3["total"], past_end["total"]} == {5}
    assert (page2["limit"], page2["offset"]) == (2, 2)


def test_ordering_is_deterministic_for_identical_timestamps(
    api_client: TestClient, alice: dict, seed_paper: SeedPaper
) -> None:
    ws = _workspace(api_client, alice)
    ids = [seed_paper(ws, minutes=0) for _ in range(4)]  # same created_at
    expected = sorted(ids, reverse=True)  # tie-break: id DESC (UUID text order == uuid order)

    first = [p["id"] for p in api_client.get(_papers_url(ws), headers=alice).json()["items"]]
    paged = [
        p["id"]
        for offset in (0, 2)
        for p in api_client.get(_papers_url(ws), params={"limit": 2, "offset": offset}, headers=alice).json()["items"]
    ]
    assert first == expected
    assert paged == expected


def test_list_only_returns_papers_of_requested_workspace(
    api_client: TestClient, alice: dict, seed_paper: SeedPaper
) -> None:
    ws_a = _workspace(api_client, alice, "A")
    ws_b = _workspace(api_client, alice, "B")
    in_a = seed_paper(ws_a)
    seed_paper(ws_b)

    body = api_client.get(_papers_url(ws_a), headers=alice).json()
    assert [p["id"] for p in body["items"]] == [in_a]
    assert body["total"] == 1


# --- 5. Get one -------------------------------------------------------------------------


def test_get_paper(api_client: TestClient, alice: dict, seed_paper: SeedPaper) -> None:
    ws = _workspace(api_client, alice)
    paper_id = seed_paper(
        ws,
        title="BERT",
        publication_year=2019,
        doi="10.18653/v1/N19-1423",
        authors='[{"name": "Jacob Devlin", "affiliation": "Google"}]',
    )

    response = api_client.get(_paper_url(paper_id), headers=alice)

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == paper_id
    assert body["workspace_id"] == ws
    assert body["title"] == "BERT"
    assert body["publication_year"] == 2019
    assert body["authors"] == [{"name": "Jacob Devlin", "affiliation": "Google"}]


def test_storage_key_is_never_exposed(api_client: TestClient, alice: dict, seed_paper: SeedPaper) -> None:
    ws = _workspace(api_client, alice)
    paper_id = seed_paper(ws)
    single = api_client.get(_paper_url(paper_id), headers=alice)
    listing = api_client.get(_papers_url(ws), headers=alice)
    for response in (single, listing):
        assert "storage_key" not in response.text
        assert f"papers/{paper_id}.pdf" not in response.text


# A realistic raw processing error: internal path, host, library and traceback fragments.
INTERNAL_STORAGE_KEY = "tenant-7/2026/11/0b9e-internal-storage.pdf"
INTERNAL_ERROR = (
    "Traceback (most recent call last):\n"
    '  File "/srv/synthora/app/workers/extract.py", line 88, in run\n'
    "fitz.FileDataError: cannot open /var/lib/synthora/storage/tenant-7/2026/11/0b9e-internal-storage.pdf "
    "(GROBID http://grobid.internal:8070 timed out)"
)
LEAK_MARKERS = (
    "error_message",
    "storage_key",
    INTERNAL_STORAGE_KEY,
    "Traceback",
    "/srv/synthora",
    "/var/lib/synthora",
    "grobid.internal",
    "FileDataError",
    "extract.py",
)


def test_internal_error_details_and_paths_never_exposed(
    api_client: TestClient, alice: dict, seed_paper: SeedPaper, query: Callable
) -> None:
    ws = _workspace(api_client, alice)
    paper_id = seed_paper(
        ws, status="failed", error_message=INTERNAL_ERROR, storage_key=INTERNAL_STORAGE_KEY, title="Broken PDF"
    )

    responses = {
        "get_single": api_client.get(_paper_url(paper_id), headers=alice),
        "list": api_client.get(_papers_url(ws), headers=alice),
        "patch": api_client.patch(_paper_url(paper_id), json={"title": "Broken PDF (renamed)"}, headers=alice),
    }

    for name, response in responses.items():
        assert response.status_code == 200, name
        for marker in LEAK_MARKERS:
            assert marker not in response.text, f"{name} leaked {marker!r}"
    # The client still learns that processing failed...
    assert responses["get_single"].json()["status"] == "failed"
    assert responses["list"].json()["items"][0]["status"] == "failed"
    # ...and the detailed error is kept server-side, unchanged, for operators.
    row = query("SELECT error_message, storage_key FROM papers WHERE id = :id", {"id": paper_id})[0]
    assert row.error_message == INTERNAL_ERROR
    assert row.storage_key == INTERNAL_STORAGE_KEY


def test_openapi_paper_schema_omits_internal_fields(api_client: TestClient) -> None:
    schemas = api_client.get("/openapi.json").json()["components"]["schemas"]
    properties = set(schemas["PaperRead"]["properties"])
    assert {"storage_key", "error_message"}.isdisjoint(properties)
    assert {"id", "status", "title", "authors", "doi"} <= properties


# --- 6-9. Metadata updates --------------------------------------------------------------


def test_update_title(api_client: TestClient, alice: dict, seed_paper: SeedPaper) -> None:
    paper_id = seed_paper(_workspace(api_client, alice), title="Old", abstract="Keep me")
    response = api_client.patch(_paper_url(paper_id), json={"title": "  New Title  "}, headers=alice)
    assert response.status_code == 200
    assert response.json()["title"] == "New Title"
    assert response.json()["abstract"] == "Keep me"  # partial update leaves other fields alone
    assert api_client.get(_paper_url(paper_id), headers=alice).json()["title"] == "New Title"


def test_update_abstract(api_client: TestClient, alice: dict, seed_paper: SeedPaper) -> None:
    paper_id = seed_paper(_workspace(api_client, alice), title="Keep")
    response = api_client.patch(_paper_url(paper_id), json={"abstract": "We propose..."}, headers=alice)
    assert response.status_code == 200
    assert response.json()["abstract"] == "We propose..."
    assert response.json()["title"] == "Keep"


def test_update_multiple_fields(api_client: TestClient, alice: dict, seed_paper: SeedPaper) -> None:
    paper_id = seed_paper(_workspace(api_client, alice))
    before = api_client.get(_paper_url(paper_id), headers=alice).json()
    payload = {
        "title": "ResNet",
        "abstract": "Deeper networks.",
        "authors": [{"name": "Kaiming He", "affiliation": "Microsoft Research"}, {"name": "Xiangyu Zhang"}],
        "publication_year": 2016,
        "doi": "https://doi.org/10.1109/CVPR.2016.90",
    }

    response = api_client.patch(_paper_url(paper_id), json=payload, headers=alice)

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "ResNet"
    assert body["abstract"] == "Deeper networks."
    assert body["authors"] == [
        {"name": "Kaiming He", "affiliation": "Microsoft Research"},
        {"name": "Xiangyu Zhang", "affiliation": None},
    ]
    assert body["publication_year"] == 2016
    assert body["doi"] == "10.1109/CVPR.2016.90"  # URL prefix stripped
    assert body["updated_at"] > before["updated_at"]
    # Processing state is untouched by metadata edits.
    for field in ("status", "sha256", "file_size_bytes", "workspace_id", "original_filename"):
        assert body[field] == before[field]


def test_clear_nullable_metadata(
    api_client: TestClient, alice: dict, seed_paper: SeedPaper, query: Callable
) -> None:
    paper_id = seed_paper(
        _workspace(api_client, alice),
        title="T", abstract="A", publication_year=2020, doi="10.1000/xyz", authors='[{"name": "X"}]',
    )
    payload = {"title": None, "abstract": None, "authors": None, "publication_year": None, "doi": None}

    response = api_client.patch(_paper_url(paper_id), json=payload, headers=alice)

    assert response.status_code == 200
    assert all(response.json()[k] is None for k in payload)
    # authors must be SQL NULL, not the JSON value 'null'.
    rows = query("SELECT authors IS NULL AS sql_null FROM papers WHERE id = :id", {"id": paper_id})
    assert rows[0].sql_null is True


def test_update_authors_to_empty_list(api_client: TestClient, alice: dict, seed_paper: SeedPaper) -> None:
    paper_id = seed_paper(_workspace(api_client, alice))
    response = api_client.patch(_paper_url(paper_id), json={"authors": []}, headers=alice)
    assert response.status_code == 200
    assert response.json()["authors"] == []


# --- 10-13. Delete, 404s, invalid ids ---------------------------------------------------


def test_delete_paper_then_404(
    api_client: TestClient, alice: dict, seed_paper: SeedPaper, query: Callable
) -> None:
    ws = _workspace(api_client, alice)
    paper_id = seed_paper(ws)
    other = seed_paper(ws, minutes=1)
    section_id = str(uuid.uuid4())
    query(
        "INSERT INTO paper_sections (id, paper_id, order_index, text, page_start, page_end) "
        "VALUES (:sid, :pid, 0, 'intro', 1, 1)",
        {"sid": section_id, "pid": paper_id},
    )

    response = api_client.delete(_paper_url(paper_id), headers=alice)

    assert response.status_code == 204
    assert response.content == b""
    assert api_client.get(_paper_url(paper_id), headers=alice).status_code == 404
    assert api_client.delete(_paper_url(paper_id), headers=alice).status_code == 404  # idempotent 404
    assert query("SELECT count(*) AS n FROM paper_sections WHERE id = :id", {"id": section_id})[0].n == 0
    remaining = api_client.get(_papers_url(ws), headers=alice).json()
    assert [p["id"] for p in remaining["items"]] == [other]
    assert remaining["total"] == 1


@pytest.mark.parametrize("method", ["get", "patch", "delete"])
def test_nonexistent_paper_returns_404(api_client: TestClient, alice: dict, method: str) -> None:
    kwargs = {"json": {"title": "x"}} if method == "patch" else {}
    response = getattr(api_client, method)(_paper_url(str(uuid.uuid4())), headers=alice, **kwargs)
    assert response.status_code == 404
    assert response.json()["error"] == {"code": "NOT_FOUND", "message": "Paper not found.", "details": None}


def test_nonexistent_workspace_listing_returns_404(api_client: TestClient, alice: dict) -> None:
    response = api_client.get(_papers_url(str(uuid.uuid4())), headers=alice)
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Workspace not found."


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("get", "/api/v1/papers/not-a-uuid"),
        ("patch", "/api/v1/papers/not-a-uuid"),
        ("delete", "/api/v1/papers/not-a-uuid"),
        ("get", "/api/v1/workspaces/not-a-uuid/papers"),
    ],
)
def test_invalid_uuid_returns_422(api_client: TestClient, alice: dict, method: str, url: str) -> None:
    kwargs = {"json": {"title": "x"}} if method == "patch" else {}
    response = getattr(api_client, method)(url, headers=alice, **kwargs)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# --- 14. Authentication -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/workspaces/{ws}/papers"),
        ("get", "/papers/{paper}"),
        ("patch", "/papers/{paper}"),
        ("delete", "/papers/{paper}"),
    ],
)
@pytest.mark.parametrize("auth", [None, "Bearer not-a-real-token"])
def test_unauthenticated_requests_return_401(
    api_client: TestClient, alice: dict, seed_paper: SeedPaper, method: str, path: str, auth: str | None
) -> None:
    ws = _workspace(api_client, alice)
    paper_id = seed_paper(ws)
    url = "/api/v1" + path.format(ws=ws, paper=paper_id)
    headers = {"Authorization": auth} if auth else {}
    kwargs = {"json": {"title": "Hijacked"}} if method == "patch" else {}

    response = getattr(api_client, method)(url, headers=headers, **kwargs)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_FAILED"
    assert response.headers["www-authenticate"] == "Bearer"
    # Nothing changed.
    assert api_client.get(_paper_url(paper_id), headers=alice).json()["title"] is None


# --- 15-18. Cross-user isolation --------------------------------------------------------


def test_other_user_cannot_get_update_or_delete(
    api_client: TestClient, alice: dict, bob: dict, seed_paper: SeedPaper
) -> None:
    paper_id = seed_paper(_workspace(api_client, alice), title="Alice's paper")
    nonexistent = api_client.get(_paper_url(str(uuid.uuid4())), headers=bob)

    responses = {
        "get": api_client.get(_paper_url(paper_id), headers=bob),
        "patch": api_client.patch(_paper_url(paper_id), json={"title": "Bob was here"}, headers=bob),
        "delete": api_client.delete(_paper_url(paper_id), headers=bob),
    }

    for name, response in responses.items():
        assert response.status_code == 404, name
        # Indistinguishable from a paper that does not exist at all.
        assert response.json() == nonexistent.json(), name
    after = api_client.get(_paper_url(paper_id), headers=alice)
    assert after.status_code == 200
    assert after.json()["title"] == "Alice's paper"


def test_other_users_papers_never_appear_in_listing(
    api_client: TestClient, alice: dict, bob: dict, seed_paper: SeedPaper
) -> None:
    alice_ws = _workspace(api_client, alice)
    bob_ws = _workspace(api_client, bob)
    seed_paper(alice_ws)
    bob_paper = seed_paper(bob_ws)

    # Bob cannot list Alice's workspace at all (404, same as a missing workspace).
    response = api_client.get(_papers_url(alice_ws), headers=bob)
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Workspace not found."
    # Bob's own listing contains only his paper.
    body = api_client.get(_papers_url(bob_ws), headers=bob).json()
    assert [p["id"] for p in body["items"]] == [bob_paper]
    assert body["total"] == 1


# --- 19-23. Validation of PATCH bodies --------------------------------------------------


def test_empty_patch_rejected(api_client: TestClient, alice: dict, seed_paper: SeedPaper) -> None:
    paper_id = seed_paper(_workspace(api_client, alice))
    response = api_client.patch(_paper_url(paper_id), json={}, headers=alice)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_unknown_fields_rejected(api_client: TestClient, alice: dict, seed_paper: SeedPaper) -> None:
    paper_id = seed_paper(_workspace(api_client, alice))
    for payload in ({"keywords": ["nlp"]}, {"title": "ok", "venue": "NeurIPS"}):
        assert api_client.patch(_paper_url(paper_id), json=payload, headers=alice).status_code == 422


@pytest.mark.parametrize("field", sorted(INTERNAL_FIELDS))
def test_internal_fields_cannot_be_modified(
    api_client: TestClient, alice: dict, seed_paper: SeedPaper, field: str
) -> None:
    ws = _workspace(api_client, alice)
    paper_id = seed_paper(ws)
    before = api_client.get(_paper_url(paper_id), headers=alice).json()

    response = api_client.patch(
        _paper_url(paper_id), json={"title": "ok", field: INTERNAL_FIELDS[field]}, headers=alice
    )

    assert response.status_code == 422
    loc = response.json()["error"]["details"][0]["loc"]
    assert loc == ["body", field]
    # The whole request was rejected: not even the valid title was applied.
    assert api_client.get(_paper_url(paper_id), headers=alice).json() == before


def test_moving_paper_to_another_workspace_is_rejected(
    api_client: TestClient, alice: dict, seed_paper: SeedPaper, query: Callable
) -> None:
    ws_a = _workspace(api_client, alice, "A")
    ws_b = _workspace(api_client, alice, "B")
    paper_id = seed_paper(ws_a)
    response = api_client.patch(_paper_url(paper_id), json={"workspace_id": ws_b}, headers=alice)
    assert response.status_code == 422
    assert str(query("SELECT workspace_id FROM papers WHERE id = :id", {"id": paper_id})[0].workspace_id) == ws_a


@pytest.mark.parametrize("year", [1799, 2101, 0, -5, "twenty", 2020.5])
def test_invalid_publication_year_rejected(
    api_client: TestClient, alice: dict, seed_paper: SeedPaper, year: Any
) -> None:
    paper_id = seed_paper(_workspace(api_client, alice))
    response = api_client.patch(_paper_url(paper_id), json={"publication_year": year}, headers=alice)
    assert response.status_code == 422


@pytest.mark.parametrize("year", [1800, 2100])
def test_publication_year_bounds_accepted(
    api_client: TestClient, alice: dict, seed_paper: SeedPaper, year: int
) -> None:
    paper_id = seed_paper(_workspace(api_client, alice))
    response = api_client.patch(_paper_url(paper_id), json={"publication_year": year}, headers=alice)
    assert response.status_code == 200
    assert response.json()["publication_year"] == year


@pytest.mark.parametrize(
    "payload",
    [
        {"title": ""},
        {"title": "   "},
        {"title": "x" * 1001},
        {"abstract": "x" * 20_001},
        {"doi": "not-a-doi"},
        {"doi": "11.1234/abc"},
        {"doi": "10.12/too-short-registrant"},
        {"authors": [{"affiliation": "No name"}]},
        {"authors": [{"name": ""}]},
        {"authors": [{"name": "A", "email": "a@x.com"}]},
        {"authors": "Jane Doe"},
        {"authors": [{"name": f"Author {i}"} for i in range(501)]},
    ],
)
def test_invalid_metadata_rejected(
    api_client: TestClient, alice: dict, seed_paper: SeedPaper, payload: dict
) -> None:
    paper_id = seed_paper(_workspace(api_client, alice))
    response = api_client.patch(_paper_url(paper_id), json=payload, headers=alice)
    assert response.status_code == 422


@pytest.mark.parametrize("raw", ["10.1000/XYZ.123", "doi:10.1000/XYZ.123", "http://dx.doi.org/10.1000/XYZ.123"])
def test_doi_normalisation(api_client: TestClient, alice: dict, seed_paper: SeedPaper, raw: str) -> None:
    paper_id = seed_paper(_workspace(api_client, alice))
    response = api_client.patch(_paper_url(paper_id), json={"doi": raw}, headers=alice)
    assert response.status_code == 200
    assert response.json()["doi"] == "10.1000/XYZ.123"


# --- 24. Pagination validation ----------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [{"limit": 0}, {"limit": 101}, {"limit": -1}, {"offset": -1}, {"limit": "ten"}, {"offset": 100_001}],
)
def test_invalid_pagination_rejected(api_client: TestClient, alice: dict, params: dict) -> None:
    ws = _workspace(api_client, alice)
    response = api_client.get(_papers_url(ws), params=params, headers=alice)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_max_limit_accepted(api_client: TestClient, alice: dict) -> None:
    ws = _workspace(api_client, alice)
    response = api_client.get(_papers_url(ws), params={"limit": 100}, headers=alice)
    assert response.status_code == 200
    assert response.json()["limit"] == 100


# --- Repository layer: the SQL itself enforces ownership --------------------------------
# The service also checks workspace ownership before listing (defence in depth). These
# tests call the repository directly so that removing its owner filter is caught too.


async def test_repository_never_touches_papers_of_another_owner(db_session: AsyncSession) -> None:
    owner = User(email="owner@example.com", hashed_password="x")
    intruder = User(email="intruder@example.com", hashed_password="x")
    db_session.add_all([owner, intruder])
    await db_session.flush()
    workspace = Workspace(owner_id=owner.id, name="Owned")
    db_session.add(workspace)
    await db_session.flush()
    paper = Paper(
        workspace_id=workspace.id,
        original_filename="p.pdf",
        storage_key=f"papers/{uuid.uuid4()}.pdf",
        sha256=hashlib.sha256(b"p").hexdigest(),
        file_size_bytes=1,
        title="Original",
    )
    db_session.add(paper)
    await db_session.commit()
    # Plain values: expired ORM attributes cannot be lazy-loaded under async SQLAlchemy.
    paper_id, workspace_id, owner_id, intruder_id = paper.id, workspace.id, owner.id, intruder.id
    repo = PaperRepository(db_session)

    assert await repo.get_owned(paper_id, intruder_id) is None
    assert await repo.list_in_workspace(workspace_id, intruder_id, limit=10, offset=0) == ([], 0)
    assert await repo.update_owned(paper_id, intruder_id, {"title": "Hijacked"}) is None
    assert await repo.delete_owned(paper_id, intruder_id) is False
    await db_session.commit()

    # Owner still sees the untouched paper (re-read from the database).
    db_session.expire_all()
    owned = await repo.get_owned(paper_id, owner_id)
    assert owned is not None and owned.title == "Original"
    assert (await repo.list_in_workspace(workspace_id, owner_id, limit=10, offset=0))[1] == 1
