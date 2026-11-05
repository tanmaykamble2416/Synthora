"""PDF text extraction engine (PyMuPDF). Pure and synchronous: no database, no FastAPI.

extract_pdf(path) opens one PDF, reads every page in order and returns a
DocumentExtraction with one PageExtraction per physical page, numbered from 1.
It can be called from a request (via the thread pool) or, from Day 10 on, from a
Celery worker without changes.

Design notes:
  * Reading order: page.get_text("text") follows the PDF content stream, which for
    LaTeX-produced two-column papers is normally column by column. sort=True is NOT
    used: sorting by position interleaves the two columns line by line.
  * Ligatures (U+FB01 "ﬁ", ...) are expanded by MuPDF (TEXT_PRESERVE_LIGATURES off).
    Unicode compatibility normalisation is not used, as it would rewrite maths.
  * A page that triggers MuPDF errors AND yields no text is treated as unreadable:
    the whole extraction fails (failure_code "extraction_error") instead of
    presenting partial content as complete. A page with warnings but real text is
    kept; the text is genuine evidence.
  * Thread safety: PyMuPDF does not support concurrent use from several threads,
    and its warning store is process-global. All extraction in this process is
    therefore serialised by _MUPDF_LOCK (one PDF at a time per worker process).
  * No OCR is performed. Pages with images and (almost) no text are flagged needs_ocr.
"""

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from app.services.pdf_text_cleaner import clean_page_text, count_significant_chars, count_words

logger = logging.getLogger(__name__)

# Fewer non-whitespace characters than this -> "low_text".
LOW_TEXT_CHAR_THRESHOLD = 50
TEXT_FLAGS = pymupdf.TEXTFLAGS_TEXT & ~pymupdf.TEXT_PRESERVE_LIGATURES

_MUPDF_LOCK = threading.Lock()
# MuPDF would otherwise print errors straight to stderr. They are collected with
# TOOLS.mupdf_warnings() instead and logged in a controlled, size-limited form.
pymupdf.TOOLS.mupdf_display_errors(False)
pymupdf.TOOLS.mupdf_display_warnings(False)
# PyMuPDF otherwise prints a one-time advertisement for its optional layout package
# to stdout the first time table detection runs.
pymupdf.no_recommend_layout()


class ExtractionFailure(Exception):
    """A handled, expected failure. `code` is user-safe; `detail` is internal only."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class PageExtraction:
    page_number: int  # 1-based, as in the source document
    text: str
    char_count: int
    word_count: int
    extraction_status: str  # "text" | "low_text" | "empty"
    needs_ocr: bool
    image_count: int


@dataclass(frozen=True)
class DocumentExtraction:
    pages: tuple[PageExtraction, ...]
    was_repaired: bool

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def count(self, status: str) -> int:
        return sum(1 for p in self.pages if p.extraction_status == status)

    @property
    def needs_ocr_pages(self) -> int:
        return sum(1 for p in self.pages if p.needs_ocr)

    @property
    def total_characters(self) -> int:
        return sum(p.char_count for p in self.pages)

    @property
    def total_words(self) -> int:
        return sum(p.word_count for p in self.pages)


def classify_page(text: str, image_count: int, threshold: int = LOW_TEXT_CHAR_THRESHOLD) -> tuple[str, bool]:
    """Return (extraction_status, needs_ocr) for a cleaned page text."""
    significant = count_significant_chars(text)
    if significant == 0:
        status = "empty"
    elif significant < threshold:
        status = "low_text"
    else:
        status = "text"
    return status, status != "text" and image_count > 0


def extract_pdf(path: Path, *, max_pages: int, low_text_threshold: int = LOW_TEXT_CHAR_THRESHOLD) -> DocumentExtraction:
    """Extract every page of the PDF at `path`. Raises ExtractionFailure for handled cases."""
    with _MUPDF_LOCK:
        pymupdf.TOOLS.reset_mupdf_warnings()
        doc = _open(path)
        with doc:  # always closes the document, also on errors
            if doc.needs_pass:
                raise ExtractionFailure("pdf_encrypted", "document requires a password")
            page_total = doc.page_count
            if page_total == 0:
                raise ExtractionFailure("pdf_corrupted", "document has no pages")
            if page_total > max_pages:
                raise ExtractionFailure("too_many_pages", f"{page_total} pages > limit {max_pages}")
            was_repaired = bool(doc.is_repaired)
            open_warnings = pymupdf.TOOLS.mupdf_warnings(reset=True)
            if was_repaired:
                logger.warning("PDF needed repair on open: %s", _summarise(open_warnings))
            pages = tuple(_extract_page(doc, index, low_text_threshold) for index in range(page_total))
    return DocumentExtraction(pages=pages, was_repaired=was_repaired)


def _open(path: Path) -> pymupdf.Document:
    if not path.is_file():
        raise ExtractionFailure("file_missing", "stored file not found")
    try:
        return pymupdf.open(path, filetype="pdf")
    except pymupdf.FileDataError as exc:  # also covers EmptyFileError
        raise ExtractionFailure("pdf_corrupted", f"cannot open: {type(exc).__name__}") from exc
    except RuntimeError as exc:  # other MuPDF errors on open
        raise ExtractionFailure("pdf_corrupted", f"cannot open: {type(exc).__name__}") from exc


def _extract_page(doc: pymupdf.Document, index: int, threshold: int) -> PageExtraction:
    page_number = index + 1
    try:
        page = doc.load_page(index)
        raw = page.get_text("text", flags=TEXT_FLAGS)
        image_count = len(page.get_images(full=False))
    except Exception as exc:  # any MuPDF failure on a page fails the whole document
        raise ExtractionFailure("extraction_error", f"page {page_number}: {type(exc).__name__}") from exc

    text = clean_page_text(raw)
    warnings = pymupdf.TOOLS.mupdf_warnings(reset=True)
    if warnings:
        if count_significant_chars(text) == 0:
            raise ExtractionFailure("extraction_error", f"page {page_number} unreadable: {_summarise(warnings)}")
        logger.warning("MuPDF warnings on page %d (text kept): %s", page_number, _summarise(warnings))

    status, needs_ocr = classify_page(text, image_count, threshold)
    return PageExtraction(
        page_number=page_number,
        text=text,
        char_count=len(text),
        word_count=count_words(text),
        extraction_status=status,
        needs_ocr=needs_ocr,
        image_count=image_count,
    )


def _summarise(warnings: str, limit: int = 200) -> str:
    """One line, bounded length. MuPDF messages describe PDF objects, not file paths."""
    flat = " | ".join(line.strip() for line in warnings.splitlines() if line.strip())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def detect_table_regions(path: Path, *, page_count: int) -> list[list[tuple[float, float, float, float]]]:
    """Bounding boxes (x0, y0, x1, y1, PDF points, origin top-left) of the tables PyMuPDF's
    table finder detects, per page. Used only to recognise table text in GROBID's output;
    it never fails an extraction: any problem gives "no tables" for that page (or all pages).

    Pages that are rotated or whose crop box does not start at (0, 0) are skipped, because
    their coordinates may not line up with GROBID's. Costs roughly 0.1 s per page."""
    regions: list[list[tuple[float, float, float, float]]] = [[] for _ in range(page_count)]
    try:
        with _MUPDF_LOCK:
            with pymupdf.open(path, filetype="pdf") as doc:
                for index in range(min(page_count, doc.page_count)):
                    try:
                        page = doc.load_page(index)
                        if page.rotation != 0 or page.cropbox.x0 != 0 or page.cropbox.y0 != 0:
                            continue
                        regions[index] = [tuple(round(v, 2) for v in t.bbox) for t in page.find_tables().tables]
                    except Exception as exc:  # table detection is best effort
                        logger.warning("Table detection skipped on page %d: %s", index + 1, type(exc).__name__)
                pymupdf.TOOLS.mupdf_warnings(reset=True)
    except Exception as exc:
        logger.warning("Table detection skipped: %s", type(exc).__name__)
        return [[] for _ in range(page_count)]
    return regions
