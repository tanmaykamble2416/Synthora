"""Builds test PDFs in memory with PyMuPDF. No binary fixtures are committed to Git.

Tests write these bytes only into pytest's tmp_path (or upload them into the
per-test tmp storage root), never into real storage.
"""

from collections.abc import Sequence
from dataclasses import dataclass

import pymupdf

# Bundled with PyMuPDF; covers Greek, maths operators, accents and CJK (not U+211D "ℝ").
UNICODE_FONT = pymupdf.Font("cjk")


@dataclass(frozen=True)
class PageSpec:
    text: str | None = None      # None -> no text layer at all
    image: bool = False          # add a raster image (simulates a scanned page)
    unicode_font: bool = False   # use the bundled Unicode font instead of Helvetica


def build_pdf(pages: Sequence[PageSpec | str | None]) -> bytes:
    doc = pymupdf.open()
    try:
        for spec in pages:
            spec = spec if isinstance(spec, PageSpec) else PageSpec(text=spec)
            page = doc.new_page()
            if spec.image:
                pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 80), False)
                pix.clear_with(180)
                page.insert_image(pymupdf.Rect(40, 40, 560, 800), pixmap=pix)
            if spec.text:
                if spec.unicode_font:
                    page.insert_font(fontname="UNI", fontbuffer=UNICODE_FONT.buffer)
                    page.insert_textbox(pymupdf.Rect(50, 50, 550, 800), spec.text, fontname="UNI", fontsize=11)
                else:
                    page.insert_textbox(pymupdf.Rect(50, 50, 550, 800), spec.text, fontsize=11)
        return doc.tobytes()
    finally:
        doc.close()


def paragraph(label: str, words: int = 40) -> str:
    """Deterministic page text with plenty of characters (well above the low-text threshold)."""
    return f"{label}. " + " ".join(f"{label.lower()}word{i}" for i in range(words))


def two_column_pdf() -> bytes:
    """Left column written first; the right column starts slightly higher on the page."""
    doc = pymupdf.open()
    try:
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(50, 100, 290, 700), "LEFT-1 alpha\nLEFT-2 beta\nLEFT-3 gamma")
        page.insert_textbox(pymupdf.Rect(310, 90, 550, 700), "RIGHT-1 delta\nRIGHT-2 epsilon")
        return doc.tobytes()
    finally:
        doc.close()


def table_pdf(rows: Sequence[Sequence[str]]) -> bytes:
    doc = pymupdf.open()
    try:
        page = doc.new_page()
        for r, row in enumerate(rows):
            for c, cell in enumerate(row):
                page.insert_text((72 + c * 150, 100 + r * 20), cell)
        return doc.tobytes()
    finally:
        doc.close()


def encrypted_pdf(*, user_password: str | None, owner_password: str = "owner-secret") -> bytes:
    doc = pymupdf.open()
    try:
        doc.new_page().insert_textbox(pymupdf.Rect(50, 50, 550, 800), paragraph("Confidential"))
        kwargs = {"encryption": pymupdf.PDF_ENCRYPT_AES_256, "owner_pw": owner_password, "permissions": 0}
        if user_password:
            kwargs["user_pw"] = user_password
        return doc.tobytes(**kwargs)
    finally:
        doc.close()


def damaged_page_pdf(damaged_page: int = 2, total: int = 3) -> bytes:
    """Valid PDF whose page `damaged_page` has a corrupt (non-zlib) Flate content stream."""
    doc = pymupdf.open()
    try:
        for i in range(total):
            doc.new_page().insert_textbox(pymupdf.Rect(50, 50, 550, 800), paragraph(f"Page{i + 1}"))
        xref = doc[damaged_page - 1].get_contents()[0]
        doc.update_stream(xref, b"\x00\x01not-a-zlib-stream" * 20, compress=False)
        doc.xref_set_key(xref, "Filter", "/FlateDecode")
        return doc.tobytes()
    finally:
        doc.close()


def zero_page_pdf() -> bytes:
    """Syntactically valid PDF with an empty page tree (PyMuPDF refuses to save 0 pages)."""
    return (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n"
    )


CORRUPTED_PDF = b"%PDF-1.7\nthis is not really a pdf body\n%%EOF\n"
