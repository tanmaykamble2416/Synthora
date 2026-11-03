"""Day 5: POST /papers/{id}/extract, GET /papers/{id}/pages[/{n}].

Papers are uploaded through the real Day 4 endpoint into the per-test tmp storage
root, then extracted. Only the guarded test database is used.
"""

import asyncio
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.repositories.paper_repository import PaperRepository
from app.services import extraction_service
from tests.integration.conftest import build_api_client, register_and_login
from tests.pdf_factory import (
    CORRUPTED_PDF,
    PageSpec,
    build_pdf,
    damaged_page_pdf,
    encrypted_pdf,
    paragraph,
)

LEAK_MARKERS = ("storage_key", "error_message", "papers/20", "Traceback", "zlib", "requires a password")


async def _run(url: URL, sql: str, params: dict[str, Any]) -> list[Any]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            assert (await conn.execute(text("SELECT current_database()"))).scalar_one().endswith("_test")
            result = await conn.execute(text(sql), params)
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


@pytest.fixture
def query(test_db_url: URL) -> Callable[..., list[Any]]:
    return lambda sql, **params: asyncio.run(_run(test_db_url, sql, params))


@pytest.fixture
def alice(api_client: TestClient) -> dict[str, str]:
    return register_and_login(api_client, "alice@example.com")


@pytest.fixture
def bob(api_client: TestClient) -> dict[str, str]:
    return register_and_login(api_client, "bob@example.com")


@pytest.fixture
def lenient_client(monkeypatch: pytest.MonkeyPatch, test_db_name: str, storage_root: Path) -> Iterator[TestClient]:
    with build_api_client(monkeypatch, test_db_name, storage_root, raise_server_exceptions=False) as client:
        yield client


def new_workspace(client: TestClient, headers: dict, name: str = "Survey") -> str:
    response = client.post("/api/v1/workspaces", json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def upload(client: TestClient, headers: dict, data: bytes, workspace_id: str | None = None) -> str:
    ws = workspace_id or new_workspace(client, headers, name=f"ws-{uuid.uuid4().hex[:8]}")
    response = client.post(
        f"/api/v1/workspaces/{ws}/papers", files={"file": ("paper.pdf", data, "application/pdf")}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def extract(client: TestClient, headers: dict, paper_id: str):
    return client.post(f"/api/v1/papers/{paper_id}/extract", headers=headers)


def pages(client: TestClient, headers: dict, paper_id: str, **params: Any):
    return client.get(f"/api/v1/papers/{paper_id}/pages", headers=headers, params=params)


def page_rows(query: Callable, paper_id: str | None = None) -> list[Any]:
    if paper_id is None:
        return query("SELECT paper_id, page_number FROM paper_pages ORDER BY paper_id, page_number")
    return query(
        "SELECT page_number, text, char_count, word_count, extraction_status, needs_ocr FROM paper_pages "
        "WHERE paper_id = :pid ORDER BY page_number", pid=paper_id,
    )


def paper_row(query: Callable, paper_id: str) -> Any:
    return query(
        "SELECT status, failure_code, error_message, page_count, processed_at, storage_key "
        "FROM papers WHERE id = :pid", pid=paper_id,
    )[0]


def assert_no_leaks(response: Any, storage_root: Path, *extra: str) -> None:
    for marker in (*LEAK_MARKERS, str(storage_root), *extra):
        assert marker not in response.text, f"leaked {marker!r}"


MIXED_PDF = build_pdf([
    paragraph("Introduction"),        # 1 text
    None,                             # 2 blank
    PageSpec(image=True),             # 3 scanned
    paragraph("Conclusion", 60),      # 4 text
])


# === Successful extraction, persistence, status ===========================================


def test_successful_extraction_persists_pages_and_updates_status(
    api_client: TestClient, alice: dict, query: Callable, storage_root: Path
) -> None:
    paper_id = upload(api_client, alice, MIXED_PDF)
    assert paper_row(query, paper_id).status == "uploaded"

    response = extract(api_client, alice, paper_id)

    assert response.status_code == 200, response.text
    summary = response.json()
    assert summary["paper_id"] == paper_id
    assert summary["status"] == "processed" and summary["failure_code"] is None
    assert (summary["page_count"], summary["text_pages"], summary["empty_pages"], summary["needs_ocr_pages"]) == (4, 2, 2, 1)
    assert summary["processed_at"] is not None
    assert_no_leaks(response, storage_root)

    rows = page_rows(query, paper_id)
    assert [r.page_number for r in rows] == [1, 2, 3, 4]
    assert rows[0].text.startswith("Introduction.") and rows[3].text.startswith("Conclusion.")
    assert (rows[1].extraction_status, rows[1].needs_ocr, rows[1].text) == ("empty", False, "")
    assert (rows[2].extraction_status, rows[2].needs_ocr) == ("empty", True)
    assert summary["total_characters"] == sum(r.char_count for r in rows)
    assert summary["total_words"] == sum(r.word_count for r in rows)

    paper = paper_row(query, paper_id)
    assert (paper.status, paper.page_count, paper.failure_code, paper.error_message) == ("processed", 4, None, None)
    detail = api_client.get(f"/api/v1/papers/{paper_id}", headers=alice).json()
    assert (detail["status"], detail["page_count"], detail["failure_code"]) == ("processed", 4, None)
    assert detail["processed_at"] is not None


def test_pages_endpoint_returns_ordered_one_based_pages(api_client: TestClient, alice: dict, storage_root: Path) -> None:
    paper_id = upload(api_client, alice, MIXED_PDF)
    extract(api_client, alice, paper_id)

    body = pages(api_client, alice, paper_id).json()
    assert body["total"] == 4 and [p["page_number"] for p in body["items"]] == [1, 2, 3, 4]
    assert set(body["items"][0]) == {"page_number", "text", "char_count", "word_count", "extraction_status", "needs_ocr"}

    second_page = pages(api_client, alice, paper_id, limit=2, offset=2).json()
    assert [p["page_number"] for p in second_page["items"]] == [3, 4]

    single = api_client.get(f"/api/v1/papers/{paper_id}/pages/4", headers=alice)
    assert single.status_code == 200
    assert single.json()["text"].startswith("Conclusion.")
    assert_no_leaks(single, storage_root)


def test_fully_scanned_pdf_is_processed_with_every_page_flagged(
    api_client: TestClient, alice: dict, query: Callable
) -> None:
    paper_id = upload(api_client, alice, build_pdf([PageSpec(image=True)] * 3))
    summary = extract(api_client, alice, paper_id).json()
    assert summary["status"] == "processed" and summary["failure_code"] is None
    assert summary["needs_ocr_pages"] == summary["page_count"] == 3
    assert summary["total_characters"] == 0
    assert all(r.needs_ocr and r.text == "" for r in page_rows(query, paper_id))


def test_unicode_text_is_stored_intact(api_client: TestClient, alice: dict, query: Callable) -> None:
    content = "Results: α=0.05, ∑ xᵢ ≤ 1, accuracy 95.3% [12]. Résumé 中文"
    paper_id = upload(api_client, alice, build_pdf([PageSpec(text=content, unicode_font=True)]))
    extract(api_client, alice, paper_id)
    stored = page_rows(query, paper_id)[0].text
    for fragment in ("α=0.05", "≤ 1", "95.3%", "[12]", "Résumé", "中文"):
        assert fragment in stored


# === Handled failures (HTTP 200 + failure_code) ============================================


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (CORRUPTED_PDF, "pdf_corrupted"),
        (encrypted_pdf(user_password="user-secret"), "pdf_encrypted"),
        (damaged_page_pdf(damaged_page=2), "extraction_error"),
    ],
    ids=["corrupted", "encrypted", "unreadable-page"],
)
def test_handled_failures_mark_paper_failed_without_pages(
    api_client: TestClient, alice: dict, query: Callable, storage_root: Path, data: bytes, code: str
) -> None:
    paper_id = upload(api_client, alice, data)

    response = extract(api_client, alice, paper_id)

    assert response.status_code == 200
    summary = response.json()
    assert (summary["status"], summary["failure_code"], summary["page_count"]) == ("failed", code, 0)
    assert_no_leaks(response, storage_root, "user-secret")
    paper = paper_row(query, paper_id)
    assert (paper.status, paper.failure_code, paper.page_count) == ("failed", code, None)
    assert paper.error_message  # internal detail kept server-side
    assert page_rows(query, paper_id) == []  # nothing partial is stored
    detail = api_client.get(f"/api/v1/papers/{paper_id}", headers=alice)
    assert (detail.json()["status"], detail.json()["failure_code"]) == ("failed", code)
    assert_no_leaks(detail, storage_root, "user-secret")


def test_missing_stored_file(api_client: TestClient, alice: dict, query: Callable, storage_root: Path) -> None:
    paper_id = upload(api_client, alice, MIXED_PDF)
    (storage_root / paper_row(query, paper_id).storage_key).unlink()
    summary = extract(api_client, alice, paper_id).json()
    assert (summary["status"], summary["failure_code"]) == ("failed", "file_missing")


def test_legacy_row_with_invalid_storage_key(api_client: TestClient, alice: dict, query: Callable) -> None:
    paper_id = upload(api_client, alice, MIXED_PDF)
    query("UPDATE papers SET storage_key = 'manual/legacy.pdf' WHERE id = :pid", pid=paper_id)
    summary = extract(api_client, alice, paper_id).json()
    assert (summary["status"], summary["failure_code"]) == ("failed", "file_missing")


def test_too_many_pages(
    monkeypatch: pytest.MonkeyPatch, test_db_name: str, storage_root: Path, query: Callable
) -> None:
    with build_api_client(monkeypatch, test_db_name, storage_root, EXTRACTION_MAX_PAGES="2") as client:
        headers = register_and_login(client, "limits@example.com")
        paper_id = upload(client, headers, build_pdf([paragraph("P")] * 3))
        summary = extract(client, headers, paper_id).json()
    assert (summary["status"], summary["failure_code"]) == ("failed", "too_many_pages")
    assert page_rows(query, paper_id) == []


# === Re-extraction ===========================================================================


def test_reextraction_replaces_pages_without_duplicates(
    api_client: TestClient, alice: dict, query: Callable
) -> None:
    paper_id = upload(api_client, alice, MIXED_PDF)
    first = extract(api_client, alice, paper_id).json()
    first_ids = {r[0] for r in query("SELECT id FROM paper_pages WHERE paper_id = :pid", pid=paper_id)}

    second = extract(api_client, alice, paper_id).json()

    assert second["status"] == "processed"
    rows = page_rows(query, paper_id)
    assert [r.page_number for r in rows] == [1, 2, 3, 4]  # still exactly one row per page
    new_ids = {r[0] for r in query("SELECT id FROM paper_pages WHERE paper_id = :pid", pid=paper_id)}
    assert new_ids.isdisjoint(first_ids)  # replaced, not appended
    assert second["processed_at"] >= first["processed_at"]


def test_failed_reextraction_removes_previous_pages(
    api_client: TestClient, alice: dict, query: Callable, storage_root: Path
) -> None:
    paper_id = upload(api_client, alice, MIXED_PDF)
    assert extract(api_client, alice, paper_id).json()["status"] == "processed"
    (storage_root / paper_row(query, paper_id).storage_key).unlink()

    summary = extract(api_client, alice, paper_id).json()

    assert (summary["status"], summary["failure_code"]) == ("failed", "file_missing")
    assert page_rows(query, paper_id) == []  # old pages cannot be mistaken for a new success
    assert pages(api_client, alice, paper_id).json()["total"] == 0
    assert paper_row(query, paper_id).page_count is None


def test_reextraction_clears_derived_sections_and_chunks(
    api_client: TestClient, alice: dict, query: Callable
) -> None:
    paper_id = upload(api_client, alice, MIXED_PDF)
    extract(api_client, alice, paper_id)
    ws = query("SELECT workspace_id FROM papers WHERE id = :pid", pid=paper_id)[0].workspace_id
    query("INSERT INTO paper_sections (paper_id, order_index, text, page_start, page_end) VALUES (:p, 0, 'x', 1, 1)", p=paper_id)
    query("INSERT INTO paper_chunks (paper_id, workspace_id, chunk_index, text, token_count, page_start, page_end) "
          "VALUES (:p, :w, 0, 'x', 1, 1, 1)", p=paper_id, w=ws)

    extract(api_client, alice, paper_id)

    assert query("SELECT count(*) AS n FROM paper_sections WHERE paper_id = :p", p=paper_id)[0].n == 0
    assert query("SELECT count(*) AS n FROM paper_chunks WHERE paper_id = :p", p=paper_id)[0].n == 0


def test_other_papers_pages_are_untouched(api_client: TestClient, alice: dict, query: Callable) -> None:
    ws = new_workspace(api_client, alice)
    a = upload(api_client, alice, build_pdf([paragraph("A")] * 2), ws)
    b = upload(api_client, alice, build_pdf([paragraph("B")] * 3), ws)
    extract(api_client, alice, a)
    extract(api_client, alice, b)
    extract(api_client, alice, a)  # re-extract A
    assert [r.page_number for r in page_rows(query, b)] == [1, 2, 3]
    assert pages(api_client, alice, a).json()["total"] == 2


# === Concurrency and stale recovery ===========================================================


def test_concurrent_extraction_is_rejected_with_409(api_client: TestClient, alice: dict, query: Callable) -> None:
    paper_id = upload(api_client, alice, MIXED_PDF)
    query("UPDATE papers SET status = 'processing' WHERE id = :pid", pid=paper_id)  # someone else's claim

    response = extract(api_client, alice, paper_id)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"
    assert paper_row(query, paper_id).status == "processing"


def test_stale_processing_claim_can_be_recovered(api_client: TestClient, alice: dict, query: Callable) -> None:
    paper_id = upload(api_client, alice, MIXED_PDF)
    query("UPDATE papers SET status = 'processing', updated_at = now() - interval '16 minutes' WHERE id = :pid",
          pid=paper_id)  # simulates a crash 16 minutes ago (limit: 15)

    response = extract(api_client, alice, paper_id)

    assert response.status_code == 200
    assert response.json()["status"] == "processed"


def test_recent_processing_claim_is_not_stolen(api_client: TestClient, alice: dict, query: Callable) -> None:
    paper_id = upload(api_client, alice, MIXED_PDF)
    query("UPDATE papers SET status = 'processing', updated_at = now() - interval '14 minutes' WHERE id = :pid",
          pid=paper_id)
    assert extract(api_client, alice, paper_id).status_code == 409


def test_claim_refreshes_updated_at(api_client: TestClient, alice: dict, query: Callable, monkeypatch: pytest.MonkeyPatch) -> None:
    """updated_at is the stale-recovery clock: the claim itself must set it to now()."""
    paper_id = upload(api_client, alice, MIXED_PDF)
    query("UPDATE papers SET updated_at = now() - interval '2 days' WHERE id = :pid", pid=paper_id)
    seen: list[Any] = []
    real_engine = extraction_service.extract_pdf

    def spy(path: Path, **kwargs: Any):
        seen.append(query("SELECT status, now() - updated_at < interval '1 minute' AS fresh "
                          "FROM papers WHERE id = :pid", pid=paper_id)[0])
        return real_engine(path, **kwargs)

    monkeypatch.setattr(extraction_service, "extract_pdf", spy)
    extract(api_client, alice, paper_id)
    assert seen[0].status == "processing" and seen[0].fresh is True


def test_result_is_discarded_if_superseded(
    api_client: TestClient, alice: dict, query: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If another extraction finished while this one was running, this result is not written."""
    paper_id = upload(api_client, alice, MIXED_PDF)
    real_engine = extraction_service.extract_pdf

    def finish_elsewhere_first(path: Path, **kwargs: Any):
        result = real_engine(path, **kwargs)
        query("UPDATE papers SET status = 'processed' WHERE id = :pid", pid=paper_id)
        return result

    monkeypatch.setattr(extraction_service, "extract_pdf", finish_elsewhere_first)
    response = extract(api_client, alice, paper_id)

    assert response.status_code == 409
    assert page_rows(query, paper_id) == []


# === Transaction rollback and unexpected errors ===============================================


def test_persistence_failure_rolls_back_all_pages_and_marks_failed(
    lenient_client: TestClient, query: Callable, storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = register_and_login(lenient_client, "rollback@example.com")
    paper_id = upload(lenient_client, headers, MIXED_PDF)

    async def explode(self, *args: Any, **kwargs: Any):
        raise RuntimeError("db exploded at /srv/secret")  # after the pages INSERT, same transaction

    monkeypatch.setattr(PaperRepository, "mark_processed", explode)
    response = extract(lenient_client, headers, paper_id)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert_no_leaks(response, storage_root, "/srv/secret", "exploded")
    assert page_rows(query, paper_id) == []  # the INSERT of all pages was rolled back
    paper = paper_row(query, paper_id)
    assert (paper.status, paper.failure_code) == ("failed", "extraction_error")  # not stuck in processing


def test_unexpected_engine_error_is_500_and_not_left_processing(
    lenient_client: TestClient, query: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = register_and_login(lenient_client, "bug@example.com")
    paper_id = upload(lenient_client, headers, MIXED_PDF)

    def bug(path: Path, **kwargs: Any):
        raise ValueError("programming error")

    monkeypatch.setattr(extraction_service, "extract_pdf", bug)
    response = extract(lenient_client, headers, paper_id)

    assert response.status_code == 500
    assert (paper_row(query, paper_id).status, paper_row(query, paper_id).failure_code) == ("failed", "extraction_error")


# === Ownership and authentication =============================================================


def test_other_user_cannot_extract_or_read_pages(
    api_client: TestClient, alice: dict, bob: dict, query: Callable
) -> None:
    paper_id = upload(api_client, alice, MIXED_PDF)
    assert extract(api_client, alice, paper_id).json()["status"] == "processed"
    nonexistent = api_client.get(f"/api/v1/papers/{uuid.uuid4()}/pages", headers=bob)

    responses = {
        "extract": extract(api_client, bob, paper_id),
        "pages": pages(api_client, bob, paper_id),
        "page": api_client.get(f"/api/v1/papers/{paper_id}/pages/1", headers=bob),
    }

    for name, response in responses.items():
        assert response.status_code == 404, name
        assert response.json() == nonexistent.json(), name  # indistinguishable from "does not exist"
        assert "Introduction" not in response.text
    assert paper_row(query, paper_id).status == "processed"  # bob's request changed nothing
    assert len(page_rows(query, paper_id)) == 4


def test_cross_workspace_isolation_on_extraction(api_client: TestClient, alice: dict, bob: dict, query: Callable) -> None:
    bob_paper = upload(api_client, bob, MIXED_PDF)
    assert extract(api_client, alice, bob_paper).status_code == 404
    assert paper_row(query, bob_paper).status == "uploaded"  # alice could not even claim it


@pytest.mark.parametrize(
    ("method", "path"),
    [("post", "/extract"), ("get", "/pages"), ("get", "/pages/1")],
)
@pytest.mark.parametrize("auth", [None, "Bearer not.a.token"])
def test_unauthenticated_requests_return_401(
    api_client: TestClient, alice: dict, query: Callable, method: str, path: str, auth: str | None
) -> None:
    paper_id = upload(api_client, alice, MIXED_PDF)
    headers = {"Authorization": auth} if auth else {}
    response = getattr(api_client, method)(f"/api/v1/papers/{paper_id}{path}", headers=headers)
    assert response.status_code == 401
    assert paper_row(query, paper_id).status == "uploaded"


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("post", f"/api/v1/papers/{uuid.uuid4()}/extract", 404),
        ("post", "/api/v1/papers/not-a-uuid/extract", 422),
        ("get", "/api/v1/papers/not-a-uuid/pages", 422),
    ],
)
def test_missing_and_invalid_paper_ids(api_client: TestClient, alice: dict, method: str, path: str, status: int) -> None:
    assert getattr(api_client, method)(path, headers=alice).status_code == status


def test_page_number_validation(api_client: TestClient, alice: dict) -> None:
    paper_id = upload(api_client, alice, MIXED_PDF)
    extract(api_client, alice, paper_id)
    base = f"/api/v1/papers/{paper_id}/pages"
    assert api_client.get(f"{base}/0", headers=alice).status_code == 422  # 1-based
    assert api_client.get(f"{base}/-1", headers=alice).status_code == 422
    missing = api_client.get(f"{base}/5", headers=alice)
    assert missing.status_code == 404 and missing.json()["error"]["message"] == "Page not found."
    assert pages(api_client, alice, paper_id, limit=51).status_code == 422
    assert pages(api_client, alice, paper_id, offset=-1).status_code == 422


def test_pages_empty_before_extraction(api_client: TestClient, alice: dict) -> None:
    paper_id = upload(api_client, alice, MIXED_PDF)
    assert pages(api_client, alice, paper_id).json() == {"items": [], "total": 0, "limit": 20, "offset": 0}
    assert api_client.get(f"/api/v1/papers/{paper_id}/pages/1", headers=alice).status_code == 404


# === Repository layer: the SQL itself enforces ownership =======================================
# The service checks paper ownership first (defence in depth); these call the repositories
# directly so that removing an owner filter from the SQL is caught too.


async def test_repositories_never_touch_another_owners_paper(db_session: Any) -> None:
    from datetime import timedelta

    from app.models import Paper, User, Workspace
    from app.repositories.paper_page_repository import PaperPageRepository
    from app.services.pdf_extraction import PageExtraction

    owner = User(email="owner@example.com", hashed_password="x")
    intruder = User(email="intruder@example.com", hashed_password="x")
    db_session.add_all([owner, intruder])
    await db_session.flush()
    workspace = Workspace(owner_id=owner.id, name="W")
    db_session.add(workspace)
    await db_session.flush()
    paper = Paper(workspace_id=workspace.id, original_filename="p.pdf", storage_key=f"k-{uuid.uuid4()}",
                  sha256="a" * 64, file_size_bytes=1)
    db_session.add(paper)
    await db_session.flush()
    paper_id, owner_id, intruder_id = paper.id, owner.id, intruder.id
    await PaperPageRepository(db_session).insert_pages(
        paper_id, [PageExtraction(1, "secret text", 11, 2, "low_text", False, 0)]
    )
    await db_session.commit()

    papers, page_repo = PaperRepository(db_session), PaperPageRepository(db_session)
    assert await page_repo.list_owned(paper_id, intruder_id, limit=10, offset=0) == ([], 0)
    assert await page_repo.get_owned(paper_id, intruder_id, 1) is None
    assert await papers.claim_for_extraction(paper_id, intruder_id, stale_after=timedelta(minutes=15)) is None
    assert await papers.lock_status_for_update(paper_id, intruder_id) is None
    await papers.delete_extracted_content(paper_id, intruder_id)
    await db_session.commit()

    items, total = await page_repo.list_owned(paper_id, owner_id, limit=10, offset=0)
    assert total == 1 and items[0].text == "secret text"  # untouched by the intruder


# === Deletion =================================================================================


def test_deleting_paper_cascades_to_pages(api_client: TestClient, alice: dict, query: Callable) -> None:
    paper_id = upload(api_client, alice, MIXED_PDF)
    extract(api_client, alice, paper_id)
    assert api_client.delete(f"/api/v1/papers/{paper_id}", headers=alice).status_code == 204
    assert page_rows(query) == []
