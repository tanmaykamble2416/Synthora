"""Day 6 quality pass, end to end: upload -> extract -> sections/chunks API.

The PDF is generated here and contains, as printed text, the issues seen in a real GROBID
0.9.1 export: a number-only heading, a ruled comparison table, headings GROBID left inside
paragraphs, an equation taken for a heading and a figure caption. The fake GROBID returns TEI
in GROBID's shape whose coordinates are measured from that PDF (page.search_for), so table
detection and page mapping run on real geometry. Only the guarded test database is used.
"""

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pymupdf
import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_grobid_client
from app.services.grobid_client import GrobidClient
from tests.integration.conftest import build_api_client, register_and_login

NS = 'xmlns="http://www.tei-c.org/ns/1.0"'

PAGE_1_LINES = [
    (90, "II."),
    (105, "RELATED WORK OR LITERATURE STUDIES"),
    (125, "The authors of the article by Desai et al. developed a recommendation model."),
    (140, "Later studies compared several machine learning classifiers on bank data."),
    (470, "III. MOTIVATION"),
    (485, "A. Real-World Problem Perception"),
    (505, "The core motivation for this work is the cost of mismatched offers."),
    (540, "DTI = Total EMI Outflows / Monthly Salary Income(1)"),
    (560, "The savings rate measures the share of income that is retained."),
]
PAGE_2_LINES = [
    (90, "IV."),
    (105, "PROBLEM DOMAIN"),
    (125, "The problem lies between financial analytics and recommender systems."),
    (300, "Fig. 2. Bank Statement Ingestion Pipeline"),
]
TABLE_TOP, ROW_H, COLS = 300, 20, (72, 250, 430, 540)
TABLE_ROWS = [("Paper Title", "Limitations"), ("Desai et al.", "No salary detection"), ("Lian and Li", "Needs interaction data")]


def build_paper() -> bytes:
    doc = pymupdf.open()
    for lines in (PAGE_1_LINES, PAGE_2_LINES):
        page = doc.new_page(width=612, height=792)
        for y, text in lines:
            page.insert_text((72, y), text, fontsize=9)
    page = doc[0]
    for r, row in enumerate(TABLE_ROWS):
        top = TABLE_TOP + r * ROW_H
        for c, cell in enumerate(row):
            page.draw_rect(pymupdf.Rect(COLS[c], top, COLS[c + 1], top + ROW_H), color=(0, 0, 0), width=0.7)
            page.insert_text((COLS[c] + 4, top + 14), cell, fontsize=9)
    data = doc.tobytes()
    doc.close()
    return data


def coords(doc: pymupdf.Document, page_no: int, text: str) -> str:
    rects = doc[page_no - 1].search_for(text)
    assert rects, f"{text!r} not found on page {page_no}"
    r = rects[0]
    return f"{page_no},{r.x0:.2f},{r.y0:.2f},{r.width:.2f},{r.height:.2f}"


def s(doc, page, text, *, lookup=None) -> str:
    return f'<s coords="{coords(doc, page, lookup or text)}">{text}</s>'


def build_tei(pdf: bytes) -> bytes:
    """What GROBID produced for such a layout (see the Day 6 quality-pass audit)."""
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    cells = " ".join(cell for row in TABLE_ROWS for cell in row)
    table_sentence = '<s coords="' + ";".join(coords(doc, 1, cell) for row in TABLE_ROWS for cell in row) + f'">{cells}</s>'
    tei = f"""<TEI {NS}><text><body>
<div {NS}><head coords="{coords(doc, 1, 'II.')}">II.</head>
  <p>{s(doc, 1, 'RELATED WORK OR LITERATURE STUDIES The authors of the article by Desai et al. developed a recommendation model.', lookup='The authors of the article')}
     {s(doc, 1, 'Later studies compared several machine learning classifiers on bank data.')}</p>
  <p>{table_sentence}{s(doc, 1, 'III. MOTIVATION A. Real-World Problem Perception', lookup='III. MOTIVATION')}</p>
  <p>{s(doc, 1, 'The core motivation for this work is the cost of mismatched offers.')}</p></div>
<div {NS}><head coords="{coords(doc, 1, 'DTI =')}">DTI = Total EMI Outflows / Monthly Salary Income(1)</head>
  <p>{s(doc, 1, 'The savings rate measures the share of income that is retained.')}</p></div>
<div {NS}><head coords="{coords(doc, 2, 'PROBLEM DOMAIN')}">PROBLEM DOMAIN</head>
  <p>{s(doc, 2, 'The problem lies between financial analytics and recommender systems.')}</p></div>
<figure {NS} xml:id="fig_0" coords="{coords(doc, 2, 'Fig. 2.')}"><head>Fig. 2 .</head><figDesc>Fig. 2. Bank Statement Ingestion Pipeline</figDesc></figure>
</body></text></TEI>"""
    doc.close()
    return tei.encode()


PDF = build_paper()
TEI = build_tei(PDF)


@pytest.fixture
def client_with_grobid(monkeypatch: pytest.MonkeyPatch, test_db_name: str, storage_root: Path):
    with build_api_client(monkeypatch, test_db_name, storage_root) as client:
        client.app.dependency_overrides[get_grobid_client] = lambda: GrobidClient(
            "http://grobid.invalid:8070", timeout_seconds=5,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=TEI)),
        )
        yield client


def extract_paper(client: TestClient, email: str) -> tuple[dict, str, dict]:
    headers = register_and_login(client, email)
    ws = client.post("/api/v1/workspaces", json={"name": "W"}, headers=headers).json()["id"]
    paper_id = client.post(
        f"/api/v1/workspaces/{ws}/papers", files={"file": ("p.pdf", PDF, "application/pdf")}, headers=headers
    ).json()["id"]
    summary = client.post(f"/api/v1/papers/{paper_id}/extract", headers=headers).json()
    return headers, paper_id, summary


def get(client: TestClient, headers: dict, paper_id: str, kind: str) -> list[dict]:
    response = client.get(f"/api/v1/papers/{paper_id}/{kind}", headers=headers, params={"limit": 50})
    assert response.status_code == 200, response.text
    return response.json()["items"]


def test_outline_is_repaired_from_pdf_evidence(client_with_grobid: TestClient) -> None:
    headers, paper_id, summary = extract_paper(client_with_grobid, "outline@example.com")
    assert (summary["status"], summary["structure_source"]) == ("processed", "grobid")
    sections = get(client_with_grobid, headers, paper_id, "sections")
    assert [(x["section_number"], x["heading"], x["level"], x["section_type"]) for x in sections] == [
        ("II.", "RELATED WORK OR LITERATURE STUDIES", 1, "related_work"),
        ("III.", "MOTIVATION", 1, "other"),
        ("A.", "Real-World Problem Perception", 2, "other"),
        ("IV.", "PROBLEM DOMAIN", 1, "other"),
    ]
    assert sections[1]["text"] == ""  # heading-only parent kept in the outline
    assert [(x["page_start"], x["page_end"]) for x in sections] == [(1, 1), (1, 1), (1, 1), (2, 2)]


def test_table_is_a_separate_chunk_and_formulas_captions_are_kept(client_with_grobid: TestClient) -> None:
    headers, paper_id, _ = extract_paper(client_with_grobid, "chunks@example.com")
    chunks = get(client_with_grobid, headers, paper_id, "chunks")
    tables = [c for c in chunks if c["content_type"] == "table"]
    assert len(tables) == 1
    assert tables[0]["text"] == "Paper Title Limitations Desai et al. No salary detection Lian and Li Needs interaction data"
    assert (tables[0]["section_number"], tables[0]["section_heading"], tables[0]["page_start"]) == (
        "II.", "RELATED WORK OR LITERATURE STUDIES", 1,
    )
    prose = " ".join(c["text"] for c in chunks if c["content_type"] == "text")
    assert "Paper Title" not in prose  # table cells are never mixed into prose chunks
    assert prose.count("DTI = Total EMI Outflows / Monthly Salary Income(1)") == 1  # formula kept once
    assert prose.count("Fig. 2. Bank Statement Ingestion Pipeline") == 1  # caption kept once
    motivation = next(c for c in chunks if c["section_heading"] == "Real-World Problem Perception")
    assert "The savings rate measures" in motivation["text"]  # the equation's text stays in its real section
    assert all(c["token_count_method"] == "word_proxy" for c in chunks)


def test_no_source_text_lost_or_duplicated(client_with_grobid: TestClient) -> None:
    import re

    headers, paper_id, _ = extract_paper(client_with_grobid, "integrity@example.com")
    sections = get(client_with_grobid, headers, paper_id, "sections")
    chunks = get(client_with_grobid, headers, paper_id, "chunks")
    tokens = lambda t: re.findall(r"[^\W_]+", (t or "").lower())  # noqa: E731
    stored = [w for x in sections for w in tokens(x["section_number"]) + tokens(x["heading"])]
    stored += [w for c in chunks for w in tokens(c["text"])]
    source = [w for _, line in PAGE_1_LINES + PAGE_2_LINES for w in tokens(line)]
    source += [w for row in TABLE_ROWS for cell in row for w in tokens(cell)]
    assert sorted(stored) == sorted(source)  # every printed word exactly once


def test_table_detection_can_be_switched_off(monkeypatch: pytest.MonkeyPatch, test_db_name: str, storage_root: Path) -> None:
    with build_api_client(monkeypatch, test_db_name, storage_root, EXTRACTION_DETECT_TABLES="false") as client:
        client.app.dependency_overrides[get_grobid_client] = lambda: GrobidClient(
            "http://grobid.invalid:8070", timeout_seconds=5,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=TEI)),
        )
        headers, paper_id, _ = extract_paper(client, "notables@example.com")
        chunks = get(client, headers, paper_id, "chunks")
    assert {c["content_type"] for c in chunks} == {"text"}


def test_repair_failure_keeps_grobid_structure(client_with_grobid: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import extraction_service

    def broken(*args: Any, **kwargs: Any):
        raise RuntimeError("repair bug")

    monkeypatch.setattr(extraction_service, "repair_structure", broken)
    headers, paper_id, summary = extract_paper(client_with_grobid, "fallback@example.com")
    assert (summary["status"], summary["structure_source"], summary["grobid_outcome"]) == ("processed", "grobid", "used")
    sections = get(client_with_grobid, headers, paper_id, "sections")
    assert sections[0]["section_number"] == "II." and sections[0]["heading"] is None  # GROBID's own, unrepaired


def test_page_fallback_chunks_are_text(monkeypatch: pytest.MonkeyPatch, test_db_name: str, storage_root: Path) -> None:
    with build_api_client(monkeypatch, test_db_name, storage_root) as client:
        headers, paper_id, summary = extract_paper(client, "fallbackpages@example.com")
        chunks = get(client, headers, paper_id, "chunks")
    assert summary["structure_source"] == "page_fallback"
    assert chunks and all(c["content_type"] == "text" and c["section_number"] is None for c in chunks)
