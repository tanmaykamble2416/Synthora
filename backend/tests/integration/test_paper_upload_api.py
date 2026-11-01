"""Day 4: POST /api/v1/workspaces/{id}/papers (PDF upload) and physical file deletion.

Every test uses an isolated tmp_path storage root (fixture `storage_root`) and the
guarded test database. Nothing is written to the real storage directory.
"""

import asyncio
import hashlib
import logging
import os
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.repositories.paper_repository import PaperRepository
from app.services.paper_service import PaperService
from app.services.storage_service import (
    CLEANUP_DIR_NAME,
    PENDING_DELETIONS_FILE,
    STORAGE_KEY_PATTERN,
    TEMP_DIR_NAME,
    StorageService,
)
from tests.integration.conftest import build_api_client, register_and_login

MIB = 1024 * 1024


def make_pdf(size: int = 4096, seed: str = "") -> bytes:
    """Bytes that start with a valid PDF header (content is irrelevant until Day 5)."""
    header = b"%PDF-1.7\n%" + seed.encode() + b"\n"
    body = hashlib.sha256(seed.encode()).digest() * (size // 32 + 1)
    trailer = b"\n%%EOF\n"
    return (header + body)[: size - len(trailer)] + trailer


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
def small_client(monkeypatch: pytest.MonkeyPatch, test_db_name: str, storage_root: Path) -> Iterator[TestClient]:
    """Same app with MAX_UPLOAD_SIZE_MB=1, for boundary tests."""
    with build_api_client(monkeypatch, test_db_name, storage_root, MAX_UPLOAD_SIZE_MB="1") as client:
        yield client


@pytest.fixture
def lenient_client(monkeypatch: pytest.MonkeyPatch, test_db_name: str, storage_root: Path) -> Iterator[TestClient]:
    """Returns the app's real 500 response instead of re-raising the server exception."""
    with build_api_client(monkeypatch, test_db_name, storage_root, raise_server_exceptions=False) as client:
        yield client


def workspace(client: TestClient, headers: dict[str, str], name: str = "Survey") -> str:
    response = client.post("/api/v1/workspaces", json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def upload_url(workspace_id: str) -> str:
    return f"/api/v1/workspaces/{workspace_id}/papers"


def upload(
    client: TestClient,
    workspace_id: str,
    headers: dict[str, str] | None,
    data: bytes,
    filename: str = "paper.pdf",
    content_type: str | None = "application/pdf",
) -> httpx2.Response:
    file_tuple = (filename, data, content_type) if content_type is not None else (filename, data)
    return client.post(upload_url(workspace_id), files={"file": file_tuple}, headers=headers or {})


def stored_files(root: Path) -> list[Path]:
    papers = root / "papers"
    return sorted(p for p in papers.rglob("*") if p.is_file()) if papers.exists() else []


def temp_files(root: Path) -> list[Path]:
    temp = root / TEMP_DIR_NAME
    return list(temp.iterdir()) if temp.exists() else []


def paper_rows(query: Callable[..., list[Any]]) -> list[Any]:
    return query("SELECT id, workspace_id, storage_key, sha256, file_size_bytes, status, page_count, "
                 "original_filename FROM papers ORDER BY created_at")


def assert_no_leaks(response: httpx2.Response, storage_root: Path, *extra: str) -> None:
    for marker in ("storage_key", "error_message", str(storage_root), "papers/20", TEMP_DIR_NAME,
                   "Traceback", *extra):
        assert marker not in response.text, f"response leaked {marker!r}"


# === 1-5. Authentication and authorisation ==============================================


def test_unauthenticated_upload_returns_401(api_client: TestClient, alice: dict, storage_root: Path) -> None:
    ws = workspace(api_client, alice)
    response = upload(api_client, ws, None, make_pdf())
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_FAILED"
    assert stored_files(storage_root) == [] and temp_files(storage_root) == []


def test_invalid_token_returns_401(api_client: TestClient, alice: dict, storage_root: Path) -> None:
    ws = workspace(api_client, alice)
    response = upload(api_client, ws, {"Authorization": "Bearer not.a.token"}, make_pdf())
    assert response.status_code == 401
    assert stored_files(storage_root) == []


def test_cannot_upload_into_another_users_workspace(
    api_client: TestClient, alice: dict, bob: dict, storage_root: Path, query: Callable
) -> None:
    alice_ws = workspace(api_client, alice)
    response = upload(api_client, alice_ws, bob, make_pdf())
    assert response.status_code == 404
    assert response.json()["error"] == {"code": "NOT_FOUND", "message": "Workspace not found.", "details": None}
    assert paper_rows(query) == [] and stored_files(storage_root) == []


def test_ownership_is_checked_before_the_body_is_validated(api_client: TestClient, alice: dict, bob: dict) -> None:
    # Garbage content into someone else's workspace: 404 (ownership), not 400/415 (content),
    # which shows the body is only looked at after authorisation.
    alice_ws = workspace(api_client, alice)
    assert upload(api_client, alice_ws, bob, b"MZ not a pdf", filename="x.exe", content_type="text/plain").status_code == 404
    assert upload(api_client, alice_ws, None, b"MZ not a pdf", filename="x.exe").status_code == 401


def test_nonexistent_workspace_returns_404(api_client: TestClient, alice: dict) -> None:
    assert upload(api_client, str(uuid.uuid4()), alice, make_pdf()).status_code == 404


def test_invalid_workspace_uuid_returns_422(api_client: TestClient, alice: dict) -> None:
    response = upload(api_client, "not-a-uuid", alice, make_pdf())
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# === 6, 17-19, 25-30, 36-38. Successful upload and its consistency ======================


def test_valid_pdf_upload_stores_file_and_creates_row(
    api_client: TestClient, alice: dict, storage_root: Path, query: Callable
) -> None:
    ws = workspace(api_client, alice)
    data = make_pdf(50_000, seed="attention")

    response = upload(api_client, ws, alice, data, filename="Attention Is All You Need.pdf")

    assert response.status_code == 201, response.text
    body = response.json()
    assert response.headers["location"] == f"/api/v1/papers/{body['id']}"
    assert body["workspace_id"] == ws
    assert body["original_filename"] == "Attention Is All You Need.pdf"
    assert body["sha256"] == hashlib.sha256(data).hexdigest()
    assert body["file_size_bytes"] == len(data)
    assert body["status"] == "uploaded"
    for field in ("page_count", "title", "abstract", "authors", "publication_year", "doi", "processed_at"):
        assert body[field] is None, field  # nothing fabricated

    rows = paper_rows(query)
    assert len(rows) == 1
    row = rows[0]
    assert str(row.id) == body["id"]
    assert row.sha256 == hashlib.sha256(data).hexdigest()
    assert row.file_size_bytes == len(data)
    assert row.status == "uploaded" and row.page_count is None
    assert row.original_filename == "Attention Is All You Need.pdf"
    # Server-generated key; the original filename is never used on disk.
    assert STORAGE_KEY_PATTERN.fullmatch(row.storage_key)
    files = stored_files(storage_root)
    assert files == [storage_root / row.storage_key]
    assert files[0].read_bytes() == data
    assert "Attention" not in files[0].name
    assert temp_files(storage_root) == []
    assert_no_leaks(response, storage_root, row.storage_key)


def test_uploaded_paper_is_listed_and_readable_without_leaks(
    api_client: TestClient, alice: dict, storage_root: Path, query: Callable
) -> None:
    ws = workspace(api_client, alice)
    paper_id = upload(api_client, ws, alice, make_pdf()).json()["id"]
    key = paper_rows(query)[0].storage_key

    single = api_client.get(f"/api/v1/papers/{paper_id}", headers=alice)
    listing = api_client.get(upload_url(ws), headers=alice)

    assert single.status_code == listing.status_code == 200
    assert listing.json()["total"] == 1
    for response in (single, listing):
        assert_no_leaks(response, storage_root, key)


def test_uppercase_pdf_extension_works(api_client: TestClient, alice: dict) -> None:
    response = upload(api_client, workspace(api_client, alice), alice, make_pdf(), filename="PAPER.PDF")
    assert response.status_code == 201
    assert response.json()["original_filename"] == "PAPER.PDF"


def test_client_path_in_filename_is_reduced_to_basename(api_client: TestClient, alice: dict, storage_root: Path) -> None:
    response = upload(api_client, workspace(api_client, alice), alice, make_pdf(), filename="..\\..\\..\\evil\\x.pdf")
    assert response.status_code == 201
    assert response.json()["original_filename"] == "x.pdf"
    assert not (storage_root.parent / "evil").exists()


def test_client_cannot_choose_storage_key(api_client: TestClient, alice: dict, query: Callable) -> None:
    ws = workspace(api_client, alice)
    response = api_client.post(
        upload_url(ws),
        files={"file": ("paper.pdf", make_pdf(), "application/pdf")},
        data={"storage_key": "../../evil.pdf"},
        headers=alice,
    )
    assert response.status_code == 400  # extra form fields are not accepted at all
    assert paper_rows(query) == []


# === 7-10, 13. File validation ==========================================================


@pytest.mark.parametrize("filename", ["malware.exe", "archive.zip", "report.docx", "notes.txt", "paper.pdf.exe"])
def test_invalid_extension_rejected(api_client: TestClient, alice: dict, storage_root: Path, filename: str) -> None:
    response = upload(api_client, workspace(api_client, alice), alice, make_pdf(), filename=filename)
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"
    assert stored_files(storage_root) == [] and temp_files(storage_root) == []


@pytest.mark.parametrize(
    "content",
    [
        b"MZ\x90\x00" + os.urandom(5000),                    # .exe renamed to .pdf
        b"PK\x03\x04" + os.urandom(5000),                    # .zip/.docx renamed to .pdf
        b"<html><script>alert(1)</script></html>",
        b"Just some text pretending to be a PDF",
        b"  %PDF-1.7\n" + os.urandom(100),                   # leading bytes: strict policy
    ],
)
def test_invalid_pdf_signature_rejected(
    api_client: TestClient, alice: dict, storage_root: Path, query: Callable, content: bytes
) -> None:
    response = upload(api_client, workspace(api_client, alice), alice, content)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"
    assert "%PDF-" in response.json()["error"]["message"]
    assert paper_rows(query) == [] and stored_files(storage_root) == [] and temp_files(storage_root) == []


@pytest.mark.parametrize("content_type", ["text/plain", "application/zip", "image/png", "text/html"])
def test_incompatible_mime_type_rejected(
    api_client: TestClient, alice: dict, storage_root: Path, content_type: str
) -> None:
    response = upload(api_client, workspace(api_client, alice), alice, make_pdf(), content_type=content_type)
    assert response.status_code == 415
    assert stored_files(storage_root) == [] and temp_files(storage_root) == []


def test_empty_file_rejected(api_client: TestClient, alice: dict, storage_root: Path, query: Callable) -> None:
    response = upload(api_client, workspace(api_client, alice), alice, b"")
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "The uploaded file is empty."
    assert paper_rows(query) == [] and temp_files(storage_root) == []


def test_octet_stream_accepted_only_with_pdf_signature(
    api_client: TestClient, alice: dict, storage_root: Path
) -> None:
    ws = workspace(api_client, alice)
    good = upload(api_client, ws, alice, make_pdf(seed="good"), content_type="application/octet-stream")
    bad = upload(api_client, ws, alice, b"PK\x03\x04 zip content", content_type="application/octet-stream")
    assert good.status_code == 201
    assert bad.status_code == 400
    assert len(stored_files(storage_root)) == 1 and temp_files(storage_root) == []


@pytest.mark.parametrize(
    ("kwargs", "status"),
    [
        ({"json": {"file": "x"}}, 415),                                                     # not multipart
        ({"content": b"x", "headers": {"Content-Type": "multipart/form-data"}}, 400),       # no boundary
        ({"files": {"document": ("p.pdf", b"%PDF-1.7 x", "application/pdf")}}, 400),        # wrong field
        ({"files": [("file", ("a.pdf", b"%PDF-1.7 a", "application/pdf")),
                    ("file", ("b.pdf", b"%PDF-1.7 b", "application/pdf"))]}, 400),          # two files
        ({"data": {"file": "not a file"}}, 415),                                             # urlencoded form
        ({"content": b'--b\r\nContent-Disposition: form-data; name="file"\r\n\r\ntext only\r\n--b--\r\n',
          "headers": {"Content-Type": "multipart/form-data; boundary=b"}}, 400),             # text part, not a file
        ({}, 415),                                                                           # no body
    ],
)
def test_malformed_uploads_rejected(
    api_client: TestClient, alice: dict, storage_root: Path, kwargs: dict, status: int
) -> None:
    ws = workspace(api_client, alice)
    headers = {**alice, **kwargs.pop("headers", {})}
    response = api_client.post(upload_url(ws), headers=headers, **kwargs)
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == {400: "BAD_REQUEST", 415: "UNSUPPORTED_MEDIA_TYPE"}[status]
    # Parser internals (configured limits, library messages) are never echoed.
    assert "Maximum number" not in response.text and "multipart data" not in response.text
    assert stored_files(storage_root) == [] and temp_files(storage_root) == []


# === 11. Size limits (MAX_UPLOAD_SIZE_MB=1 here) =========================================


def test_file_of_exactly_the_limit_is_accepted(small_client: TestClient, storage_root: Path) -> None:
    headers = register_and_login(small_client, "big@example.com")
    data = make_pdf(1 * MIB, seed="exact")
    response = upload(small_client, workspace(small_client, headers), headers, data)
    assert response.status_code == 201, response.text
    assert response.json()["file_size_bytes"] == 1 * MIB


def test_file_one_byte_over_the_limit_is_413(small_client: TestClient, storage_root: Path, query: Callable) -> None:
    headers = register_and_login(small_client, "big@example.com")
    response = upload(small_client, workspace(small_client, headers), headers, make_pdf(1 * MIB + 1))
    assert response.status_code == 413
    assert response.json()["error"] == {
        "code": "PAYLOAD_TOO_LARGE", "message": "The upload exceeds the maximum size of 1 MB.", "details": None
    }
    assert paper_rows(query) == [] and stored_files(storage_root) == [] and temp_files(storage_root) == []


def _multipart(data: bytes, boundary: str = "synthora-test-boundary") -> tuple[bytes, str]:
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="big.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def test_misleading_small_content_length_cannot_bypass_limit(
    small_client: TestClient, storage_root: Path, query: Callable
) -> None:
    headers = register_and_login(small_client, "big@example.com")
    body, content_type = _multipart(make_pdf(3 * MIB))
    response = small_client.post(
        upload_url(workspace(small_client, headers)),
        content=body,
        headers={**headers, "Content-Type": content_type, "Content-Length": "1000"},  # a lie
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert paper_rows(query) == [] and stored_files(storage_root) == [] and temp_files(storage_root) == []


def test_streamed_body_without_content_length_is_cut_off(
    small_client: TestClient, storage_root: Path, query: Callable
) -> None:
    headers = register_and_login(small_client, "big@example.com")
    body, content_type = _multipart(make_pdf(3 * MIB))
    pulled: list[int] = []

    def chunked() -> Iterator[bytes]:  # no Content-Length -> chunked transfer encoding
        for i in range(0, len(body), 256 * 1024):
            pulled.append(i)
            yield body[i : i + 256 * 1024]

    response = small_client.post(
        upload_url(workspace(small_client, headers)), content=chunked(),
        headers={**headers, "Content-Type": content_type},
    )
    assert response.status_code == 413
    assert paper_rows(query) == [] and stored_files(storage_root) == [] and temp_files(storage_root) == []


@pytest.mark.parametrize("declared_length", ["1000", None])
def test_body_limit_enforced_on_received_bytes_even_if_file_is_small(
    small_client: TestClient, storage_root: Path, query: Callable, declared_length: str | None
) -> None:
    """3 MiB of multipart preamble + a 1 KB PDF. The per-file check alone would accept
    this (the file is tiny); only the receive-level byte counter can reject it."""
    headers = register_and_login(small_client, "big@example.com")
    body, content_type = _multipart(make_pdf(1000))
    padded = b"p" * (3 * MIB) + b"\r\n" + body  # RFC 2046 preamble, ignored by parsers
    request_headers = {**headers, "Content-Type": content_type}
    if declared_length is not None:
        request_headers["Content-Length"] = declared_length  # lie
        content: Any = padded
    else:
        content = (padded[i : i + 256 * 1024] for i in range(0, len(padded), 256 * 1024))  # chunked

    response = small_client.post(upload_url(workspace(small_client, headers)), content=content, headers=request_headers)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert paper_rows(query) == [] and stored_files(storage_root) == [] and temp_files(storage_root) == []


def test_small_preamble_is_400_not_413(small_client: TestClient) -> None:
    """Control for the test above: the same request shape with a small preamble is
    rejected by the multipart parser as malformed (400). So the 413 above is caused by
    the size of the received body, not by the preamble."""
    headers = register_and_login(small_client, "big@example.com")
    body, content_type = _multipart(make_pdf(1000))
    response = small_client.post(
        upload_url(workspace(small_client, headers)),
        content=b"p" * 1000 + b"\r\n" + body,
        headers={**headers, "Content-Type": content_type},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"


def test_oversized_declared_content_length_rejected_immediately(small_client: TestClient, storage_root: Path) -> None:
    headers = register_and_login(small_client, "big@example.com")
    body, content_type = _multipart(make_pdf(1000))
    response = small_client.post(
        upload_url(workspace(small_client, headers)),
        content=body,
        headers={**headers, "Content-Type": content_type, "Content-Length": str(50 * MIB)},
    )
    assert response.status_code == 413
    assert stored_files(storage_root) == [] and temp_files(storage_root) == []


def test_invalid_content_length_rejected(api_client: TestClient, alice: dict) -> None:
    body, content_type = _multipart(make_pdf(1000))
    response = api_client.post(
        upload_url(workspace(api_client, alice)), content=body,
        headers={**alice, "Content-Type": content_type, "Content-Length": "abc"},
    )
    assert response.status_code == 400


# === 14-16, 30. Duplicate detection ======================================================


def test_duplicate_in_same_workspace_is_409_and_not_stored_twice(
    api_client: TestClient, alice: dict, storage_root: Path, query: Callable
) -> None:
    ws = workspace(api_client, alice)
    data = make_pdf(seed="dup")
    assert upload(api_client, ws, alice, data).status_code == 201

    second = upload(api_client, ws, alice, data, filename="renamed-copy.pdf")

    assert second.status_code == 409
    assert second.json()["error"] == {
        "code": "CONFLICT", "message": "This PDF has already been uploaded to this workspace.", "details": None
    }
    assert len(paper_rows(query)) == 1
    assert len(stored_files(storage_root)) == 1 and temp_files(storage_root) == []


def test_same_pdf_allowed_in_another_workspace(
    api_client: TestClient, alice: dict, storage_root: Path, query: Callable
) -> None:
    data = make_pdf(seed="shared")
    a = upload(api_client, workspace(api_client, alice, "A"), alice, data)
    b = upload(api_client, workspace(api_client, alice, "B"), alice, data)
    assert a.status_code == b.status_code == 201
    assert a.json()["sha256"] == b.json()["sha256"]
    rows = paper_rows(query)
    assert len(rows) == 2 and rows[0].storage_key != rows[1].storage_key  # separate physical copies
    assert len(stored_files(storage_root)) == 2


def test_race_past_precheck_is_caught_by_unique_constraint(
    api_client: TestClient, alice: dict, storage_root: Path, query: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deterministic version of the race: both requests 'pass' the pre-check, so the
    second one is stopped only by UNIQUE (workspace_id, sha256)."""
    ws = workspace(api_client, alice)
    data = make_pdf(seed="race")

    async def precheck_always_misses(self, *args: Any) -> bool:
        return False

    monkeypatch.setattr(PaperRepository, "sha256_exists_in_owned_workspace", precheck_always_misses)
    first = upload(api_client, ws, alice, data)
    second = upload(api_client, ws, alice, data)

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "CONFLICT"
    assert "uq_papers" not in second.text and "duplicate key" not in second.text
    assert len(paper_rows(query)) == 1
    # The loser's already-committed file was removed again: exactly one file remains.
    assert len(stored_files(storage_root)) == 1 and temp_files(storage_root) == []


@pytest.mark.parametrize("parallel", [2, 6])
def test_concurrent_duplicate_uploads(
    api_client: TestClient, alice: dict, storage_root: Path, query: Callable, parallel: int
) -> None:
    ws = workspace(api_client, alice)
    data = make_pdf(200_000, seed=f"concurrent-{parallel}")

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        statuses = sorted(pool.map(lambda _: upload(api_client, ws, alice, data).status_code, range(parallel)))

    assert statuses == [201] + [409] * (parallel - 1)
    assert len(paper_rows(query)) == 1
    assert len(stored_files(storage_root)) == 1 and temp_files(storage_root) == []


# === 23-24, 39. Failure cleanup ===========================================================


def test_permanent_file_removed_when_db_insert_fails(
    lenient_client: TestClient, storage_root: Path, query: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    api_client = lenient_client
    alice = register_and_login(api_client, "alice@example.com")
    ws = workspace(api_client, alice)
    seen_files: list[list[Path]] = []

    async def exploding_create(self, **kwargs: Any) -> None:
        seen_files.append(stored_files(storage_root))  # the file IS in permanent storage here
        raise RuntimeError("insert failed at /srv/secret/db.sock password=hunter2")

    monkeypatch.setattr(PaperRepository, "create", exploding_create)
    response = upload(api_client, ws, alice, make_pdf())

    assert response.status_code == 500
    assert response.json()["error"] == {
        "code": "INTERNAL_ERROR", "message": "An unexpected error occurred.", "details": None
    }
    assert_no_leaks(response, storage_root, "/srv/secret", "hunter2", "RuntimeError")
    assert len(seen_files) == 1 and len(seen_files[0]) == 1  # proves cleanup had something to remove
    assert stored_files(storage_root) == [] and temp_files(storage_root) == []
    assert paper_rows(query) == []


def test_permanent_file_removed_when_workspace_vanishes_mid_upload(
    api_client: TestClient, alice: dict, storage_root: Path, query: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real FK violation from PostgreSQL: the workspace disappears after the ownership check."""

    async def ownership_passes(self, owner_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
        return None

    monkeypatch.setattr(PaperService, "require_owned_workspace", ownership_passes)
    response = upload(api_client, str(uuid.uuid4()), alice, make_pdf())

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Workspace not found."
    assert "fk_papers" not in response.text
    assert stored_files(storage_root) == [] and temp_files(storage_root) == [] and paper_rows(query) == []


def test_storage_failure_returns_generic_500_and_cleans_temp(
    lenient_client: TestClient, storage_root: Path, query: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    api_client = lenient_client
    alice = register_and_login(api_client, "alice@example.com")
    ws = workspace(api_client, alice)

    def disk_full(self, staged: Any, key: str) -> None:
        raise OSError(28, f"No space left on device: '{self.root / key}'")

    monkeypatch.setattr(StorageService, "commit", disk_full)
    response = upload(api_client, ws, alice, make_pdf())

    assert response.status_code == 500
    assert_no_leaks(response, storage_root, "No space left")
    assert temp_files(storage_root) == [] and stored_files(storage_root) == [] and paper_rows(query) == []


# === 31-35. Delete integration ===========================================================


def test_delete_removes_row_file_and_cascades(
    api_client: TestClient, alice: dict, storage_root: Path, query: Callable
) -> None:
    ws = workspace(api_client, alice)
    paper_id = upload(api_client, ws, alice, make_pdf()).json()["id"]
    file_path = stored_files(storage_root)[0]
    query("INSERT INTO paper_sections (paper_id, order_index, text, page_start, page_end) "
          "VALUES (:pid, 0, 'Introduction', 1, 1)", pid=paper_id)
    query("INSERT INTO paper_chunks (paper_id, workspace_id, chunk_index, text, token_count, page_start, page_end) "
          "VALUES (:pid, :ws, 0, 'chunk', 1, 1, 1)", pid=paper_id, ws=ws)

    response = api_client.delete(f"/api/v1/papers/{paper_id}", headers=alice)

    assert response.status_code == 204
    assert not file_path.exists()
    assert paper_rows(query) == []
    assert query("SELECT count(*) AS n FROM paper_sections")[0].n == 0
    assert query("SELECT count(*) AS n FROM paper_chunks")[0].n == 0
    assert api_client.get(f"/api/v1/papers/{paper_id}", headers=alice).status_code == 404


def test_other_user_cannot_delete_paper_or_file(
    api_client: TestClient, alice: dict, bob: dict, storage_root: Path, query: Callable
) -> None:
    paper_id = upload(api_client, workspace(api_client, alice), alice, make_pdf()).json()["id"]
    response = api_client.delete(f"/api/v1/papers/{paper_id}", headers=bob)
    assert response.status_code == 404
    assert len(stored_files(storage_root)) == 1
    assert len(paper_rows(query)) == 1


def test_failed_file_deletion_is_logged_queued_and_retryable(
    api_client: TestClient,
    alice: dict,
    storage_root: Path,
    query: Callable,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ws = workspace(api_client, alice)
    paper_id = upload(api_client, ws, alice, make_pdf()).json()["id"]
    key = paper_rows(query)[0].storage_key
    file_path = storage_root / key

    def locked(self, storage_key: str) -> bool:
        raise PermissionError(f"[WinError 32] file in use: '{self.root / storage_key}'")

    monkeypatch.setattr(StorageService, "delete", locked)
    with caplog.at_level(logging.WARNING):
        response = api_client.delete(f"/api/v1/papers/{paper_id}", headers=alice)
    monkeypatch.undo()

    # The DB deletion stands; the client sees a normal 204 with no internals.
    assert response.status_code == 204 and response.content == b""
    assert paper_rows(query) == []
    assert file_path.exists()
    assert f"Could not delete stored file for paper {paper_id}; queued for retry" in caplog.text
    ledger = storage_root / CLEANUP_DIR_NAME / PENDING_DELETIONS_FILE
    assert key in ledger.read_text(encoding="utf-8")

    report = StorageService(storage_root).retry_pending_deletions()

    assert (report.deleted, report.still_failing) == (1, 0)
    assert not file_path.exists()
    assert not ledger.exists()
    # The deleted row was not recreated at any point.
    assert paper_rows(query) == []


def test_delete_of_row_whose_file_is_already_gone(
    api_client: TestClient, alice: dict, storage_root: Path, query: Callable, caplog: pytest.LogCaptureFixture
) -> None:
    paper_id = upload(api_client, workspace(api_client, alice), alice, make_pdf()).json()["id"]
    stored_files(storage_root)[0].unlink()
    with caplog.at_level(logging.WARNING):
        assert api_client.delete(f"/api/v1/papers/{paper_id}", headers=alice).status_code == 204
    assert "already missing" in caplog.text
    assert paper_rows(query) == []


# === Storage isolation sanity ===============================================================


def test_tests_never_use_the_real_storage_directory(api_client: TestClient, storage_root: Path) -> None:
    from app.core.config import PROJECT_ROOT, get_settings

    assert get_settings().paper_storage_root == storage_root
    assert not storage_root.is_relative_to(PROJECT_ROOT)
