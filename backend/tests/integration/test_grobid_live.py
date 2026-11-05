"""Opt-in tests against a REAL GROBID service (Day 6).

Skipped unless RUN_GROBID_TESTS=1 (and, like every integration test, RUN_DB_TESTS=1).
Start GROBID first:  docker compose up -d grobid   (wait until /api/isalive returns true)

    RUN_DB_TESTS=1 RUN_GROBID_TESTS=1 pytest tests/integration/test_grobid_live.py -v

Optional: GROBID_LIVE_PDF=<path to a real paper> also runs the full API flow on that
file and prints a short structure report (use -s to see it). The file is copied into
the per-test temporary storage only; nothing is committed or kept.

GROBID's URL comes from GROBID_URL (default http://127.0.0.1:8070).
"""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.services.grobid_client import GrobidClient
from app.services.tei_parser import parse_tei
from tests.integration.conftest import build_api_client, register_and_login
from tests.pdf_factory import build_pdf, paragraph

pytestmark = pytest.mark.skipif(os.getenv("RUN_GROBID_TESTS") != "1", reason="set RUN_GROBID_TESTS=1 to use a real GROBID")

PAGES = [
    "A Synthetic Study of Retrieval\n\nAbstract\n\n" + paragraph("Abstract", 60),
    "1 Introduction\n\n" + paragraph("Introduction", 150),
    "2 Method\n\n" + paragraph("Method", 150),
    "3 Conclusion\n\n" + paragraph("Conclusion", 80) + "\n\nReferences\n\n[1] A. Author. A study of things. Journal of Tests, 2020.",
]


def _grobid_url() -> str:
    return os.getenv("GROBID_URL", "http://127.0.0.1:8070")


async def test_real_grobid_returns_parseable_tei_with_valid_pages(tmp_path: Path) -> None:
    pdf = tmp_path / "live.pdf"
    pdf.write_bytes(build_pdf(PAGES))
    client = GrobidClient(_grobid_url(), timeout_seconds=120)
    alive, _ = await client.is_alive()
    assert alive, f"GROBID is not reachable at {_grobid_url()}"

    response = await client.process_fulltext(pdf)

    assert response.outcome == "ok", response.detail
    document = parse_tei(response.tei, page_count=len(PAGES))
    assert document.paragraph_count > 0
    for section in document.sections:
        for p in section.paragraphs:
            if p.page_start is not None:
                assert 1 <= p.page_start <= p.page_end <= len(PAGES)


def test_full_extraction_with_real_grobid(monkeypatch: pytest.MonkeyPatch, test_db_name: str, storage_root: Path) -> None:
    data = Path(os.environ["GROBID_LIVE_PDF"]).read_bytes() if os.getenv("GROBID_LIVE_PDF") else build_pdf(PAGES)
    with build_api_client(
        monkeypatch, test_db_name, storage_root, GROBID_ENABLED="true", GROBID_URL=_grobid_url()
    ) as client:
        assert get_settings().grobid_enabled
        assert client.get("/api/v1/health/grobid").json()["status"] == "ok"
        headers = register_and_login(client, "live@example.com")
        ws = client.post("/api/v1/workspaces", json={"name": "Live"}, headers=headers).json()["id"]
        upload = client.post(
            f"/api/v1/workspaces/{ws}/papers", files={"file": ("live.pdf", data, "application/pdf")}, headers=headers
        )
        assert upload.status_code == 201, upload.text
        paper_id = upload.json()["id"]

        summary = client.post(f"/api/v1/papers/{paper_id}/extract", headers=headers).json()

        assert summary["status"] == "processed"
        assert (summary["structure_source"], summary["grobid_outcome"]) == ("grobid", "used"), summary
        chunks = _all(client, headers, paper_id, "chunks", 50)
        assert chunks and all(1 <= c["page_start"] <= c["page_end"] <= summary["page_count"] for c in chunks)
        assert all(c["token_count"] <= 500 and c["token_count_method"] == "word_proxy" for c in chunks)
        _report(client, headers, paper_id, summary, chunks)


def _all(client: TestClient, headers: dict, paper_id: str, kind: str, limit: int) -> list[dict]:
    items: list[dict] = []
    while True:
        body = client.get(
            f"/api/v1/papers/{paper_id}/{kind}", headers=headers, params={"limit": limit, "offset": len(items)}
        ).json()
        items += body["items"]
        if len(items) >= body["total"]:
            return items


def _report(client: TestClient, headers: dict, paper_id: str, summary: dict, chunks: list[dict]) -> None:
    paper = client.get(f"/api/v1/papers/{paper_id}", headers=headers).json()
    sections = _all(client, headers, paper_id, "sections", 100)
    sizes = sorted(c["token_count"] for c in chunks)
    print("\n--- GROBID live extraction report ---")
    print(f"pages={summary['page_count']} sections={len(sections)} chunks={len(chunks)} references={summary['reference_count']}")
    print(f"metadata filled: {summary['metadata_fields_filled']}")
    print(f"title: {paper['title']!r}")
    print(f"authors: {[a['name'] for a in paper['authors'] or []]}")
    print(f"year={paper['publication_year']} doi={paper['doi']} venue={paper['venue']!r}")
    print(f"chunk words: min={sizes[0]} median={sizes[len(sizes) // 2]} max={sizes[-1]}")
    for s in sections:
        print(f"  [{s['section_type']:<12}] L{s['level']} p{s['page_start']}-{s['page_end']} {s['heading']!r}")
