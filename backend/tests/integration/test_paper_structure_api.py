"""Day 6: structure extraction (GROBID or page fallback), keep-last-good re-extraction,
fill-only-NULL metadata and the sections/chunks/references/health endpoints.

GROBID is replaced by a GrobidClient whose HTTP transport is an in-process
httpx MockTransport serving tests/fixtures/tei/*.xml, injected through FastAPI's
dependency_overrides. No network and no real GROBID are used here (see
test_grobid_live.py for the opt-in live test). Only the guarded test database is used.
"""

import asyncio
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.api.deps import get_grobid_client
from app.repositories.paper_repository import PaperRepository
from app.repositories.paper_structure_repository import PaperStructureRepository
from app.services import extraction_service
from app.services.grobid_client import GrobidClient
from tests.integration.conftest import build_api_client, register_and_login
from tests.pdf_factory import build_pdf, paragraph

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "tei"
FULL_TEI = (FIXTURES / "full_paper.tei.xml").read_bytes()
HEADER_ONLY_TEI = (FIXTURES / "header_only.tei.xml").read_bytes()
FAKE_GROBID_URL = "http://grobid.invalid:8070"
# Five text pages: the TEI fixture's coordinates refer to pages 1-5.
FIVE_PAGE_PDF = build_pdf([paragraph(f"Page{i}", 80) for i in range(1, 6)])
LEAK_MARKERS = ("storage_key", "error_message", "grobid.invalid", "Traceback", "/srv/secret")


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


class FakeGrobid:
    """Serves one canned response (or raises) and records every request."""

    def __init__(self) -> None:
        self.calls = 0
        self.respond: Callable[[httpx.Request], httpx.Response] = lambda r: httpx.Response(200, content=FULL_TEI)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return self.respond(request)

    def client(self) -> GrobidClient:
        return GrobidClient(
            FAKE_GROBID_URL, timeout_seconds=5, transport=httpx.MockTransport(self.handler), busy_retry_delay_seconds=0
        )


def use_grobid(client: TestClient) -> FakeGrobid:
    fake = FakeGrobid()
    client.app.dependency_overrides[get_grobid_client] = fake.client
    return fake


def disable_grobid(client: TestClient) -> None:
    client.app.dependency_overrides.pop(get_grobid_client, None)


def upload(client: TestClient, headers: dict, data: bytes = FIVE_PAGE_PDF) -> str:
    ws = client.post("/api/v1/workspaces", json={"name": "Survey"}, headers=headers).json()["id"]
    response = client.post(
        f"/api/v1/workspaces/{ws}/papers", files={"file": ("paper.pdf", data, "application/pdf")}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def extract(client: TestClient, headers: dict, paper_id: str) -> httpx.Response:
    return client.post(f"/api/v1/papers/{paper_id}/extract", headers=headers)


def listing(client: TestClient, headers: dict, paper_id: str, kind: str, **params: Any) -> dict:
    response = client.get(f"/api/v1/papers/{paper_id}/{kind}", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def paper_row(query: Callable, paper_id: str) -> Any:
    return query(
        "SELECT status, failure_code, page_count, processed_at, structure_source, grobid_outcome, "
        "last_extraction_failure_code, last_extraction_attempt_at, title, abstract, authors, publication_year, "
        "doi, venue FROM papers WHERE id = :pid", pid=paper_id,
    )[0]


def content_snapshot(query: Callable, paper_id: str) -> dict[str, list[Any]]:
    """Every derived row with its id: identical snapshots mean nothing was touched."""
    return {
        table: query(f"SELECT * FROM {table} WHERE paper_id = :pid ORDER BY id", pid=paper_id)
        for table in ("paper_pages", "paper_sections", "paper_chunks", "paper_references")
    }


def assert_no_leaks(response: Any, storage_root: Path) -> None:
    for marker in (*LEAK_MARKERS, str(storage_root)):
        assert marker not in response.text, f"leaked {marker!r}"


# === GROBID used =============================================================================


def test_grobid_extraction_stores_structure_references_and_metadata(
    api_client: TestClient, alice: dict, query: Callable, storage_root: Path
) -> None:
    fake = use_grobid(api_client)
    paper_id = upload(api_client, alice)

    response = extract(api_client, alice, paper_id)

    assert response.status_code == 200, response.text
    assert fake.calls == 1
    summary = response.json()
    assert (summary["status"], summary["paper_status"], summary["retained_previous_extraction"]) == (
        "processed", "processed", False,
    )
    assert (summary["structure_source"], summary["grobid_outcome"]) == ("grobid", "used")
    assert (summary["page_count"], summary["section_count"], summary["reference_count"]) == (5, 9, 4)
    assert summary["chunk_count"] > 0
    assert summary["metadata_fields_filled"] == ["title", "abstract", "authors", "publication_year", "doi", "venue"]
    assert_no_leaks(response, storage_root)

    row = paper_row(query, paper_id)
    assert (row.structure_source, row.grobid_outcome, row.last_extraction_failure_code) == ("grobid", "used", None)
    assert row.title == "Structured Retrieval for Literature Reviews"
    assert row.authors == [
        {"name": "Ada M Lovelace", "affiliation": "Department of Computing, Example University"},
        {"name": "Alan Turing", "affiliation": None},
    ]
    assert (row.publication_year, row.doi, row.venue) == (2021, "10.1234/synthora.test.2021", "Journal of Synthetic Research")
    assert row.processed_at == row.last_extraction_attempt_at

    detail = api_client.get(f"/api/v1/papers/{paper_id}", headers=alice).json()
    assert (detail["structure_source"], detail["grobid_outcome"], detail["venue"]) == (
        "grobid", "used", "Journal of Synthetic Research",
    )


def test_sections_endpoint_lists_ordered_typed_sections(api_client: TestClient, alice: dict) -> None:
    use_grobid(api_client)
    paper_id = upload(api_client, alice)
    extract(api_client, alice, paper_id)

    body = listing(api_client, alice, paper_id, "sections")
    assert body["total"] == 9
    items = body["items"]
    assert [s["order_index"] for s in items] == list(range(9))
    assert [s["section_type"] for s in items] == [
        "abstract", "introduction", "related_work", "methodology", "methodology",
        "experiments", "results", "conclusion", "other",
    ]
    method = items[3]
    assert (method["heading"], method["level"], method["page_start"], method["page_end"]) == ("Proposed Method", 1, 2, 3)
    assert items[4]["level"] == 2
    assert set(items[0]) == {
        "id", "order_index", "level", "section_number", "heading", "section_type", "text", "page_start", "page_end",
    }


def test_chunks_endpoint_returns_citable_chunks(api_client: TestClient, alice: dict, query: Callable) -> None:
    use_grobid(api_client)
    paper_id = upload(api_client, alice)
    summary = extract(api_client, alice, paper_id).json()

    body = listing(api_client, alice, paper_id, "chunks", limit=50)
    chunks = body["items"]
    assert body["total"] == summary["chunk_count"] == len(chunks)
    assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        assert 1 <= chunk["page_start"] <= chunk["page_end"] <= 5
        assert chunk["token_count_method"] == "word_proxy"
        assert chunk["token_count"] == len(chunk["text"].split()) <= 500
        assert chunk["section_heading"] is not None and chunk["section_id"] is not None
        assert "FOOTNOTE TEXT" not in chunk["text"]
        assert "Attention is all you need" not in chunk["text"]
    # Day 6 quality pass: the figure caption and the formula are kept, exactly once each.
    joined = " ".join(c["text"] for c in chunks)
    assert joined.count("FIGURE CAPTION TEXT IS KEPT.") == 1
    assert joined.count("L = sum of losses (1)") == 1
    # Chunks never cross sections: each chunk's section matches the section of its text.
    rows = query(
        "SELECT c.chunk_index, s.order_index FROM paper_chunks c JOIN paper_sections s "
        "ON s.id = c.section_id AND s.paper_id = c.paper_id WHERE c.paper_id = :pid ORDER BY 1", pid=paper_id,
    )
    assert [r.order_index for r in rows] == sorted(r.order_index for r in rows)  # reading order preserved
    method_chunks = [c for c in chunks if c["section_heading"] == "Proposed Method"]
    assert method_chunks[-1]["page_end"] == 3  # the paragraph continuing onto page 3


def test_references_endpoint(api_client: TestClient, alice: dict) -> None:
    use_grobid(api_client)
    paper_id = upload(api_client, alice)
    extract(api_client, alice, paper_id)

    refs = listing(api_client, alice, paper_id, "references")["items"]
    assert [r["ref_index"] for r in refs] == [0, 1, 2, 3]
    assert refs[0]["title"] == "Attention is all you need"
    assert refs[0]["authors"] == ["Ashish Vaswani", "Noam Shazeer"]
    assert (refs[0]["doi"], refs[0]["page_start"], refs[0]["page_end"]) == ("10.5555/3295222.3295349", 4, 4)
    assert refs[2]["title"] is None and refs[2]["authors"] is None and refs[2]["raw_text"]
    assert (refs[3]["doi"], refs[3]["publication_year"], refs[3]["page_start"]) == (None, None, None)


# === Fill-only-NULL metadata =================================================================


def test_manual_metadata_is_never_overwritten(api_client: TestClient, alice: dict, query: Callable) -> None:
    use_grobid(api_client)
    paper_id = upload(api_client, alice)
    edit = {"title": "My Own Title", "doi": "10.9999/mine", "authors": []}
    assert api_client.patch(f"/api/v1/papers/{paper_id}", json=edit, headers=alice).status_code == 200

    summary = extract(api_client, alice, paper_id).json()

    assert summary["metadata_fields_filled"] == ["abstract", "publication_year", "venue"]
    row = paper_row(query, paper_id)
    assert (row.title, row.doi, row.authors) == ("My Own Title", "10.9999/mine", [])  # [] is a value, kept
    assert row.venue == "Journal of Synthetic Research"


def test_reextraction_fills_nothing_already_filled(api_client: TestClient, alice: dict, query: Callable) -> None:
    use_grobid(api_client)
    paper_id = upload(api_client, alice)
    extract(api_client, alice, paper_id)
    api_client.patch(f"/api/v1/papers/{paper_id}", json={"venue": "Corrected Venue"}, headers=alice)

    summary = extract(api_client, alice, paper_id).json()

    assert summary["metadata_fields_filled"] == []
    assert paper_row(query, paper_id).venue == "Corrected Venue"


def test_cleared_field_is_filled_again(api_client: TestClient, alice: dict, query: Callable) -> None:
    use_grobid(api_client)
    paper_id = upload(api_client, alice)
    extract(api_client, alice, paper_id)
    api_client.patch(f"/api/v1/papers/{paper_id}", json={"abstract": None}, headers=alice)

    assert extract(api_client, alice, paper_id).json()["metadata_fields_filled"] == ["abstract"]
    assert paper_row(query, paper_id).abstract is not None


# === GROBID disabled / failing: page fallback (safeguard 3) ==================================


def test_disabled_grobid_uses_page_fallback(api_client: TestClient, alice: dict, query: Callable) -> None:
    disable_grobid(api_client)  # the default in tests: GROBID_ENABLED=false
    paper_id = upload(api_client, alice)

    summary = extract(api_client, alice, paper_id).json()

    assert (summary["status"], summary["structure_source"], summary["grobid_outcome"]) == (
        "processed", "page_fallback", "disabled",
    )
    assert (summary["section_count"], summary["reference_count"], summary["metadata_fields_filled"]) == (0, 0, [])
    assert summary["chunk_count"] > 0
    chunks = listing(api_client, alice, paper_id, "chunks", limit=50)["items"]
    assert all(c["section_id"] is None and c["section_heading"] is None for c in chunks)
    assert all(1 <= c["page_start"] <= c["page_end"] <= 5 for c in chunks)
    assert " ".join(c["text"] for c in chunks).startswith("Page1.")
    row = paper_row(query, paper_id)
    assert (row.title, row.authors, row.doi, row.venue) == (None, None, None, None)  # nothing invented
    assert listing(api_client, alice, paper_id, "sections")["total"] == 0
    assert listing(api_client, alice, paper_id, "references")["total"] == 0


def _raise(error: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def respond(request: httpx.Request) -> httpx.Response:
        raise error

    return respond


@pytest.mark.parametrize(
    ("respond", "outcome"),
    [
        (_raise(httpx.ConnectError("refused")), "unavailable"),
        (lambda r: httpx.Response(503), "unavailable"),
        (_raise(httpx.ReadTimeout("slow")), "timeout"),
        (lambda r: httpx.Response(500, content=b"[GENERAL] boom at /srv/secret"), "error"),
        (lambda r: httpx.Response(204), "invalid_response"),
        (lambda r: httpx.Response(200, content=FULL_TEI[:3000]), "invalid_response"),
        (lambda r: httpx.Response(200, content=HEADER_ONLY_TEI), "invalid_response"),
        (lambda r: httpx.Response(200, content=b'<!DOCTYPE x [<!ENTITY a "b">]><TEI/>'), "invalid_response"),
        (lambda r: httpx.Response(200, content=b"<html>proxy error page</html>"), "invalid_response"),
    ],
    ids=["refused", "busy", "timeout", "http-500", "no-content", "truncated", "no-body", "doctype", "not-tei"],
)
def test_grobid_problems_never_fail_the_extraction(
    api_client: TestClient, alice: dict, query: Callable, storage_root: Path, respond: Callable, outcome: str
) -> None:
    fake = use_grobid(api_client)
    fake.respond = respond
    paper_id = upload(api_client, alice)

    response = extract(api_client, alice, paper_id)

    assert response.status_code == 200
    summary = response.json()
    assert (summary["status"], summary["failure_code"]) == ("processed", None)
    assert (summary["structure_source"], summary["grobid_outcome"]) == ("page_fallback", outcome)
    assert (summary["section_count"], summary["reference_count"], summary["metadata_fields_filled"]) == (0, 0, [])
    assert summary["chunk_count"] > 0
    assert_no_leaks(response, storage_root)
    row = paper_row(query, paper_id)
    assert (row.status, row.structure_source, row.grobid_outcome, row.title) == (
        "processed", "page_fallback", outcome, None,
    )
    assert query("SELECT count(*) AS n FROM paper_references WHERE paper_id = :p", p=paper_id)[0].n == 0


def test_bug_in_structure_building_falls_back_to_pages(
    api_client: TestClient, alice: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_grobid(api_client)

    def broken(*args: Any, **kwargs: Any):
        raise RuntimeError("chunker bug")

    monkeypatch.setattr(extraction_service, "build_from_tei", broken)
    paper_id = upload(api_client, alice)
    summary = extract(api_client, alice, paper_id).json()
    assert (summary["status"], summary["structure_source"], summary["grobid_outcome"]) == (
        "processed", "page_fallback", "error",
    )
    assert summary["metadata_fields_filled"] == [] and summary["reference_count"] == 0


def test_successful_fallback_reextraction_replaces_grobid_structure(
    api_client: TestClient, alice: dict, query: Callable
) -> None:
    fake = use_grobid(api_client)
    paper_id = upload(api_client, alice)
    extract(api_client, alice, paper_id)
    fake.respond = _raise(httpx.ConnectError("refused"))

    summary = extract(api_client, alice, paper_id).json()

    # A successful extraction replaces everything atomically, including the old structure.
    assert (summary["structure_source"], summary["grobid_outcome"]) == ("page_fallback", "unavailable")
    assert listing(api_client, alice, paper_id, "sections")["total"] == 0
    assert listing(api_client, alice, paper_id, "references")["total"] == 0
    # Filled metadata is paper data, not derived content: it stays.
    assert paper_row(query, paper_id).title == "Structured Retrieval for Literature Reviews"


# === Keep the last good extraction (safeguards A and 2) ======================================


def test_previous_extraction_stays_readable_while_reextracting(
    api_client: TestClient, alice: dict, query: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_grobid(api_client)
    paper_id = upload(api_client, alice)
    extract(api_client, alice, paper_id)
    before = content_snapshot(query, paper_id)
    seen: list[Any] = []
    real_engine = extraction_service.extract_pdf

    def spy(path: Path, **kwargs: Any):
        seen.append((paper_row(query, paper_id).status, content_snapshot(query, paper_id)))
        return real_engine(path, **kwargs)

    monkeypatch.setattr(extraction_service, "extract_pdf", spy)
    extract(api_client, alice, paper_id)

    status, during = seen[0]
    assert status == "processing"
    assert during == before  # T1 claimed the paper without deleting anything


def test_t2_failure_keeps_previous_extraction_and_records_attempt(
    lenient_client: TestClient, query: Callable, storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = register_and_login(lenient_client, "t2@example.com")
    use_grobid(lenient_client)
    paper_id = upload(lenient_client, headers)
    first = extract(lenient_client, headers, paper_id).json()
    before = content_snapshot(query, paper_id)
    before_row = paper_row(query, paper_id)

    async def explode(self, *args: Any, **kwargs: Any):
        raise RuntimeError("db exploded at /srv/secret")  # the LAST step of T2, after all inserts

    monkeypatch.setattr(PaperRepository, "mark_processed", explode)
    response = extract(lenient_client, headers, paper_id)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert_no_leaks(response, storage_root)
    assert content_snapshot(query, paper_id) == before  # T2 rolled back completely
    row = paper_row(query, paper_id)
    assert (row.status, row.failure_code, row.page_count) == ("processed", None, 5)  # status restored
    assert row.last_extraction_failure_code == "extraction_error"
    assert row.processed_at == before_row.processed_at  # last success unchanged
    assert row.last_extraction_attempt_at > before_row.last_extraction_attempt_at
    assert (row.structure_source, row.grobid_outcome, row.title) == ("grobid", "used", before_row.title)
    assert lenient_client.get(f"/api/v1/papers/{paper_id}/chunks", headers=headers).json()["total"] == first["chunk_count"]


@pytest.mark.parametrize("step", ["insert_sections", "insert_chunks", "insert_references"])
def test_failure_at_any_t2_step_inserts_nothing(
    lenient_client: TestClient, query: Callable, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    headers = register_and_login(lenient_client, f"{step}@example.com")
    use_grobid(lenient_client)
    paper_id = upload(lenient_client, headers)
    extract(lenient_client, headers, paper_id)
    before = content_snapshot(query, paper_id)

    async def explode(self, *args: Any, **kwargs: Any):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(PaperStructureRepository, step, explode)
    assert extract(lenient_client, headers, paper_id).status_code == 500
    assert content_snapshot(query, paper_id) == before
    assert paper_row(query, paper_id).status == "processed"


def test_t2_failure_without_previous_extraction_marks_failed(
    lenient_client: TestClient, query: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = register_and_login(lenient_client, "first@example.com")
    paper_id = upload(lenient_client, headers)

    async def explode(self, *args: Any, **kwargs: Any):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(PaperStructureRepository, "insert_chunks", explode)
    assert extract(lenient_client, headers, paper_id).status_code == 500
    row = paper_row(query, paper_id)
    assert (row.status, row.failure_code, row.page_count, row.processed_at) == ("failed", "extraction_error", None, None)
    assert row.last_extraction_failure_code == "extraction_error"
    assert all(rows == [] for rows in content_snapshot(query, paper_id).values())


def test_handled_failure_after_grobid_extraction_keeps_everything(
    api_client: TestClient, alice: dict, query: Callable, storage_root: Path
) -> None:
    use_grobid(api_client)
    paper_id = upload(api_client, alice)
    extract(api_client, alice, paper_id)
    before = content_snapshot(query, paper_id)
    storage_key = query("SELECT storage_key FROM papers WHERE id = :p", p=paper_id)[0].storage_key
    (storage_root / storage_key).unlink()

    summary = extract(api_client, alice, paper_id).json()

    assert (summary["status"], summary["failure_code"], summary["retained_previous_extraction"]) == (
        "failed", "file_missing", True,
    )
    assert (summary["structure_source"], summary["chunk_count"]) == (None, 0)  # describes THIS attempt
    assert content_snapshot(query, paper_id) == before
    row = paper_row(query, paper_id)
    assert (row.status, row.structure_source, row.grobid_outcome) == ("processed", "grobid", "used")
    assert listing(api_client, alice, paper_id, "references")["total"] == 4


def test_successful_reextraction_after_failure_clears_last_failure(
    api_client: TestClient, alice: dict, query: Callable, storage_root: Path
) -> None:
    paper_id = upload(api_client, alice)
    data_path = storage_root / query("SELECT storage_key FROM papers WHERE id = :p", p=paper_id)[0].storage_key
    original = data_path.read_bytes()
    data_path.unlink()
    assert extract(api_client, alice, paper_id).json()["status"] == "failed"
    assert paper_row(query, paper_id).status == "failed"
    data_path.write_bytes(original)

    summary = extract(api_client, alice, paper_id).json()

    assert summary["status"] == "processed"
    row = paper_row(query, paper_id)
    assert (row.status, row.failure_code, row.last_extraction_failure_code) == ("processed", None, None)
    assert row.processed_at is not None


# === Ownership, validation, authentication ====================================================


@pytest.mark.parametrize("kind", ["sections", "chunks", "references"])
def test_other_user_gets_404(api_client: TestClient, alice: dict, bob: dict, kind: str) -> None:
    use_grobid(api_client)
    paper_id = upload(api_client, alice)
    extract(api_client, alice, paper_id)
    response = api_client.get(f"/api/v1/papers/{paper_id}/{kind}", headers=bob)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.parametrize("kind", ["sections", "chunks", "references"])
def test_structure_endpoints_require_authentication(api_client: TestClient, alice: dict, kind: str) -> None:
    paper_id = upload(api_client, alice)
    assert api_client.get(f"/api/v1/papers/{paper_id}/{kind}").status_code == 401


@pytest.mark.parametrize("kind", ["sections", "chunks", "references"])
def test_unknown_paper_is_404_and_bad_id_is_422(api_client: TestClient, alice: dict, kind: str) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    assert api_client.get(f"/api/v1/papers/{missing}/{kind}", headers=alice).status_code == 404
    assert api_client.get(f"/api/v1/papers/not-a-uuid/{kind}", headers=alice).status_code == 422


@pytest.mark.parametrize(
    ("kind", "limit"), [("sections", 101), ("chunks", 51), ("references", 101), ("chunks", 0)]
)
def test_page_size_limits(api_client: TestClient, alice: dict, kind: str, limit: int) -> None:
    paper_id = upload(api_client, alice)
    response = api_client.get(f"/api/v1/papers/{paper_id}/{kind}", headers=alice, params={"limit": limit})
    assert response.status_code == 422


def test_pagination_of_chunks(api_client: TestClient, alice: dict) -> None:
    use_grobid(api_client)
    paper_id = upload(api_client, alice)
    total = extract(api_client, alice, paper_id).json()["chunk_count"]
    first = listing(api_client, alice, paper_id, "chunks", limit=2, offset=0)
    second = listing(api_client, alice, paper_id, "chunks", limit=2, offset=2)
    assert first["total"] == second["total"] == total
    assert [c["chunk_index"] for c in first["items"] + second["items"]] == [0, 1, 2, 3]


def test_structure_is_empty_before_extraction(api_client: TestClient, alice: dict) -> None:
    paper_id = upload(api_client, alice)
    for kind in ("sections", "chunks", "references"):
        assert listing(api_client, alice, paper_id, kind) == {"items": [], "total": 0, "limit": 50 if kind != "chunks" else 20, "offset": 0}


def test_deleting_paper_cascades_to_references(api_client: TestClient, alice: dict, query: Callable) -> None:
    use_grobid(api_client)
    paper_id = upload(api_client, alice)
    extract(api_client, alice, paper_id)
    assert api_client.delete(f"/api/v1/papers/{paper_id}", headers=alice).status_code == 204
    assert all(rows == [] for rows in content_snapshot(query, paper_id).values())


def test_grobid_never_receives_the_original_filename(api_client: TestClient, alice: dict) -> None:
    fake = use_grobid(api_client)
    bodies: list[bytes] = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        return httpx.Response(200, content=FULL_TEI)

    fake.respond = respond
    ws = api_client.post("/api/v1/workspaces", json={"name": "W"}, headers=alice).json()["id"]
    paper_id = api_client.post(
        f"/api/v1/workspaces/{ws}/papers",
        files={"file": ("Confidential-Draft.pdf", FIVE_PAGE_PDF, "application/pdf")},
        headers=alice,
    ).json()["id"]
    extract(api_client, alice, paper_id)
    assert b"Confidential-Draft" not in bodies[0]
    assert b'filename="paper.pdf"' in bodies[0]


# === GROBID health ==============================================================================


def test_grobid_health_disabled(api_client: TestClient) -> None:
    disable_grobid(api_client)
    response = api_client.get("/api/v1/health/grobid")
    assert response.status_code == 200
    assert response.json() == {"status": "disabled", "latency_ms": None}


def test_grobid_health_ok(api_client: TestClient) -> None:
    fake = use_grobid(api_client)
    fake.respond = lambda r: httpx.Response(200, text="true")
    response = api_client.get("/api/v1/health/grobid")
    assert response.status_code == 200 and response.json()["status"] == "ok"


def test_grobid_health_unreachable_is_503_without_url(api_client: TestClient) -> None:
    fake = use_grobid(api_client)
    fake.respond = _raise(httpx.ConnectError("refused"))
    response = api_client.get("/api/v1/health/grobid")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "GROBID_UNAVAILABLE"
    assert "grobid.invalid" not in response.text and "8070" not in response.text


def test_enabled_setting_builds_a_real_client(
    monkeypatch: pytest.MonkeyPatch, test_db_name: str, storage_root: Path
) -> None:
    with build_api_client(
        monkeypatch, test_db_name, storage_root, GROBID_ENABLED="true", GROBID_URL="http://127.0.0.1:1"
    ) as client:
        # Port 1 refuses connections: an enabled but unreachable GROBID.
        response = client.get("/api/v1/health/grobid")
        assert response.status_code == 503
        headers = register_and_login(client, "real@example.com")
        paper_id = upload(client, headers)
        summary = extract(client, headers, paper_id).json()
    assert (summary["status"], summary["structure_source"], summary["grobid_outcome"]) == (
        "processed", "page_fallback", "unavailable",
    )


# === Repository layer: COALESCE is the last line of defence ===================================


async def test_fill_metadata_never_overwrites_at_sql_level(db_session: Any) -> None:
    """Even if the service passed a value for a filled field, the UPDATE keeps the old one."""
    from app.models import Paper, User, Workspace

    user = User(email="sql@example.com", hashed_password="x")
    db_session.add(user)
    await db_session.flush()
    workspace = Workspace(owner_id=user.id, name="W")
    db_session.add(workspace)
    await db_session.flush()
    paper = Paper(
        workspace_id=workspace.id, original_filename="p.pdf", storage_key="k-sql", sha256="c" * 64,
        file_size_bytes=1, title="Manual title", authors=[{"name": "Me", "affiliation": None}],
    )
    db_session.add(paper)
    await db_session.flush()
    paper_id, owner_id = paper.id, user.id
    await db_session.commit()

    await PaperRepository(db_session).fill_metadata(
        paper_id,
        owner_id,
        {"title": "GROBID title", "authors": [{"name": "Other", "affiliation": None}], "venue": "Filled venue"},
    )
    await db_session.commit()

    row = (await db_session.execute(
        text("SELECT title, authors, venue FROM papers WHERE id = :p"), {"p": paper_id}
    )).one()
    assert (row.title, row.authors, row.venue) == ("Manual title", [{"name": "Me", "affiliation": None}], "Filled venue")
    with pytest.raises(ValueError):
        await PaperRepository(db_session).fill_metadata(paper_id, owner_id, {"status": "processed"})
    with pytest.raises(ValueError):
        await PaperRepository(db_session).fill_metadata(paper_id, owner_id, {"title": None})
