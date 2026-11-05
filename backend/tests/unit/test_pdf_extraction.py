"""The extraction engine in isolation (no database). PDFs are generated into tmp_path."""

from pathlib import Path

import pymupdf
import pytest

from app.services import pdf_extraction
from app.services.pdf_extraction import (
    LOW_TEXT_CHAR_THRESHOLD,
    ExtractionFailure,
    classify_page,
    extract_pdf,
)
from tests.pdf_factory import (
    CORRUPTED_PDF,
    PageSpec,
    build_pdf,
    damaged_page_pdf,
    encrypted_pdf,
    paragraph,
    table_pdf,
    two_column_pdf,
    zero_page_pdf,
)


def _write(tmp_path: Path, data: bytes, name: str = "paper.pdf") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _extract(tmp_path: Path, data: bytes, **kwargs):
    return extract_pdf(_write(tmp_path, data), max_pages=kwargs.pop("max_pages", 100), **kwargs)


def _failure(tmp_path: Path, data: bytes, **kwargs) -> ExtractionFailure:
    with pytest.raises(ExtractionFailure) as exc_info:
        _extract(tmp_path, data, **kwargs)
    return exc_info.value


# --- successful extraction --------------------------------------------------------------


def test_single_page_extraction(tmp_path: Path) -> None:
    result = _extract(tmp_path, build_pdf([paragraph("Abstract")]))
    assert result.page_count == 1
    page = result.pages[0]
    assert page.page_number == 1
    assert page.text.startswith("Abstract. abstractword0 abstractword1")
    assert page.extraction_status == "text" and page.needs_ocr is False
    assert page.char_count == len(page.text)
    assert page.word_count == 41


def test_multi_page_order_and_one_based_numbering(tmp_path: Path) -> None:
    labels = [f"Section{i}" for i in range(1, 8)]
    result = _extract(tmp_path, build_pdf([paragraph(label) for label in labels]))
    assert [p.page_number for p in result.pages] == list(range(1, 8))
    for page, label in zip(result.pages, labels, strict=True):
        assert page.text.startswith(f"{label}.")  # each page's own text, in order
    assert result.total_words == sum(p.word_count for p in result.pages)
    assert result.total_characters == sum(p.char_count for p in result.pages)


def test_empty_low_text_and_image_pages_keep_their_numbers(tmp_path: Path) -> None:
    pdf = build_pdf([
        paragraph("Intro"),                         # 1 text
        None,                                       # 2 truly blank page
        "Figure 3.",                                # 3 short caption, no image
        PageSpec(image=True),                       # 4 scanned page, no text layer
        PageSpec(text="Page 5", image=True),        # 5 scan with a stamp-like line of text
        paragraph("Conclusion"),                    # 6 text
    ])
    pages = _extract(tmp_path, pdf).pages
    summary = [(p.page_number, p.extraction_status, p.needs_ocr) for p in pages]
    assert summary == [
        (1, "text", False),
        (2, "empty", False),
        (3, "low_text", False),
        (4, "empty", True),
        (5, "low_text", True),
        (6, "text", False),
    ]
    assert pages[1].text == "" and pages[1].char_count == 0 and pages[1].word_count == 0


def test_fully_scanned_pdf_is_a_successful_extraction_flagged_for_ocr(tmp_path: Path) -> None:
    result = _extract(tmp_path, build_pdf([PageSpec(image=True)] * 4))
    assert result.page_count == 4
    assert result.needs_ocr_pages == 4
    assert result.count("empty") == 4
    assert result.total_characters == 0  # nothing invented: no OCR happened


def test_unicode_text_survives(tmp_path: Path) -> None:
    text = "Unicode: α β γ ∑ ∫ ≤ ≥ é ü ö 中文 x² 95.3% [12] ℝ end"
    page = _extract(tmp_path, build_pdf([PageSpec(text=text, unicode_font=True)])).pages[0]
    for fragment in ("α β γ ∑ ∫ ≤ ≥", "é ü ö", "中文", "x²", "95.3%", "[12]"):
        assert fragment in page.text
    # The bundled font has no glyph for ℝ: MuPDF yields U+0000, which the cleaner removes.
    assert "\x00" not in page.text and "ℝ" not in page.text


def test_ligatures_are_expanded(tmp_path: Path) -> None:
    page = _extract(tmp_path, build_pdf([PageSpec(text="deﬁnition of eﬀect", unicode_font=True)])).pages[0]
    assert "definition" in page.text
    assert "ﬁ" not in page.text


def test_two_column_layout_keeps_column_order(tmp_path: Path) -> None:
    words = _extract(tmp_path, two_column_pdf()).pages[0].text.split()
    labels = [w for w in words if w.startswith(("LEFT", "RIGHT"))]
    assert labels == ["LEFT-1", "LEFT-2", "LEFT-3", "RIGHT-1", "RIGHT-2"]


def test_table_cells_and_numbers_are_preserved(tmp_path: Path) -> None:
    rows = [("Model", "Acc (%)", "F1"), ("BERT", "92.4", "0.91"), ("RoBERTa", "94.1", "0.93")]
    text = _extract(tmp_path, table_pdf(rows)).pages[0].text
    for cell in (c for row in rows for c in row):
        assert cell in text
    assert text.index("BERT") < text.index("RoBERTa")


def test_owner_password_only_pdf_is_readable(tmp_path: Path) -> None:
    result = _extract(tmp_path, encrypted_pdf(user_password=None))
    assert result.pages[0].text.startswith("Confidential.")


def test_low_text_threshold_is_configurable(tmp_path: Path) -> None:
    pdf = build_pdf(["Short but real."])
    assert _extract(tmp_path, pdf).pages[0].extraction_status == "low_text"
    assert _extract(tmp_path, pdf, low_text_threshold=5).pages[0].extraction_status == "text"


@pytest.mark.parametrize(
    ("text", "images", "expected"),
    [
        ("", 0, ("empty", False)),
        ("", 2, ("empty", True)),
        ("x" * (LOW_TEXT_CHAR_THRESHOLD - 1), 0, ("low_text", False)),
        ("x" * (LOW_TEXT_CHAR_THRESHOLD - 1), 1, ("low_text", True)),
        ("x" * LOW_TEXT_CHAR_THRESHOLD, 1, ("text", False)),
        (" \n " * 100, 1, ("empty", True)),  # whitespace does not count as text
    ],
)
def test_classify_page(text: str, images: int, expected: tuple[str, bool]) -> None:
    assert classify_page(text, images) == expected


# --- handled failures ---------------------------------------------------------------------


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ExtractionFailure) as exc_info:
        extract_pdf(tmp_path / "does-not-exist.pdf", max_pages=10)
    assert exc_info.value.code == "file_missing"


@pytest.mark.parametrize("data", [CORRUPTED_PDF, b"", b"%PDF-1.7\n" + b"\xff" * 2000])
def test_corrupted_pdf(tmp_path: Path, data: bytes) -> None:
    assert _failure(tmp_path, data).code == "pdf_corrupted"


def test_zero_page_pdf(tmp_path: Path) -> None:
    assert _failure(tmp_path, zero_page_pdf()).code == "pdf_corrupted"


def test_password_protected_pdf(tmp_path: Path) -> None:
    failure = _failure(tmp_path, encrypted_pdf(user_password="user-secret"))
    assert failure.code == "pdf_encrypted"
    assert "user-secret" not in failure.detail


def test_too_many_pages(tmp_path: Path) -> None:
    failure = _failure(tmp_path, build_pdf([paragraph("P")] * 3), max_pages=2)
    assert failure.code == "too_many_pages"


def test_one_unreadable_page_fails_the_whole_document(tmp_path: Path) -> None:
    failure = _failure(tmp_path, damaged_page_pdf(damaged_page=2, total=3))
    assert failure.code == "extraction_error"
    assert "page 2" in failure.detail


def test_exception_on_a_page_fails_the_whole_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_get_text = pymupdf.Page.get_text

    def flaky(self: pymupdf.Page, *args, **kwargs):
        if self.number == 1:  # 0-based -> page 2
            raise RuntimeError("simulated MuPDF failure")
        return real_get_text(self, *args, **kwargs)

    monkeypatch.setattr(pymupdf.Page, "get_text", flaky)
    failure = _failure(tmp_path, build_pdf([paragraph("A"), paragraph("B"), paragraph("C")]))
    assert failure.code == "extraction_error"
    assert failure.detail == "page 2: RuntimeError"


def test_failure_details_never_contain_the_file_path(tmp_path: Path) -> None:
    for data in (CORRUPTED_PDF, encrypted_pdf(user_password="u"), damaged_page_pdf()):
        assert str(tmp_path) not in _failure(tmp_path, data).detail


# --- resource handling --------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        build_pdf([paragraph("Ok")]),
        encrypted_pdf(user_password="u"),
        damaged_page_pdf(),
        build_pdf([paragraph("P")] * 3),  # with max_pages=2 -> too_many_pages
    ],
    ids=["success", "encrypted", "damaged-page", "too-many-pages"],
)
def test_document_is_always_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    opened: list[pymupdf.Document] = []
    real_open = pymupdf.open

    def tracking_open(*args, **kwargs):
        doc = real_open(*args, **kwargs)
        opened.append(doc)
        return doc

    monkeypatch.setattr(pdf_extraction.pymupdf, "open", tracking_open)
    try:
        _extract(tmp_path, data, max_pages=2)
    except ExtractionFailure:
        pass
    assert len(opened) == 1
    assert opened[0].is_closed


def test_extraction_is_serialised_by_a_process_wide_lock() -> None:
    # PyMuPDF is not thread-safe; extract_pdf must hold this lock while using MuPDF.
    assert pdf_extraction._MUPDF_LOCK.acquire(blocking=False)
    pdf_extraction._MUPDF_LOCK.release()


# --- Day 6 quality pass: PyMuPDF table regions ---------------------------------------------

from app.services.pdf_extraction import detect_table_regions  # noqa: E402


def _ruled_table_pdf(path, *, rotate: int = 0):
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 80), "Text above the table.", fontsize=9)
    for r in range(3):
        for c, (x0, x1) in enumerate(((72, 250), (250, 430))):
            page.draw_rect(pymupdf.Rect(x0, 300 + r * 20, x1, 320 + r * 20), color=(0, 0, 0), width=0.7)
            page.insert_text((x0 + 4, 314 + r * 20), f"cell {r}{c}", fontsize=9)
    doc.new_page().insert_text((72, 80), "A page without tables.")
    if rotate:
        doc[0].set_rotation(rotate)
    doc.save(path)
    doc.close()


def test_ruled_table_region_is_detected(tmp_path):
    path = tmp_path / "table.pdf"
    _ruled_table_pdf(path)
    regions = detect_table_regions(path, page_count=2)
    assert len(regions) == 2 and regions[1] == []
    (x0, y0, x1, y1), = regions[0]
    assert (round(x0), round(y0), round(x1), round(y1)) == (72, 300, 430, 360)


def test_rotated_pages_are_skipped(tmp_path):
    path = tmp_path / "rotated.pdf"
    _ruled_table_pdf(path, rotate=90)
    assert detect_table_regions(path, page_count=2) == [[], []]


def test_table_detection_never_raises(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.7 not really a pdf")
    assert detect_table_regions(path, page_count=3) == [[], [], []]
    assert detect_table_regions(tmp_path / "missing.pdf", page_count=1) == [[]]


def test_table_detection_prints_nothing(tmp_path, capsys):
    path = tmp_path / "table.pdf"
    _ruled_table_pdf(path)
    detect_table_regions(path, page_count=2)
    assert "pymupdf_layout" not in capsys.readouterr().out
