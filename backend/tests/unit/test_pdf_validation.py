import pytest

from app.core.exceptions import BadRequestError, UnsupportedMediaTypeError
from app.services.pdf_validation import (
    MAX_FILENAME_LENGTH,
    check_declared_content_type,
    require_pdf_signature,
    sanitize_filename,
)

# --- filenames ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("paper.pdf", "paper.pdf"),
        ("PAPER.PDF", "PAPER.PDF"),
        ("Mixed.Pdf", "Mixed.Pdf"),
        ("../../etc/passwd.pdf", "passwd.pdf"),
        ("C:\\Users\\me\\Desktop\\thesis.pdf", "thesis.pdf"),
        ("/var/tmp/report.pdf", "report.pdf"),
        ("  spaced name.pdf  ", "spaced name.pdf"),
        ("résumé.pdf", "résumé.pdf"),
        ("tab\tand\nnewline.pdf", "tabandnewline.pdf"),
        ("paper.exe.pdf", "paper.exe.pdf"),  # extension ok; content still has to be a PDF
    ],
)
def test_sanitize_filename(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["malware.exe", "archive.zip", "doc.docx", "notes.txt", "paper.pdf.exe", "paper", "paper.pdfx", "paper.pd"],
)
def test_non_pdf_extensions_are_unsupported(raw: str) -> None:
    with pytest.raises(UnsupportedMediaTypeError):
        sanitize_filename(raw)


def test_rtl_override_spoofing_is_neutralised() -> None:
    # Displays as "invoiceexe.pdf" but is really "invoice<RLO>fdp.exe".
    with pytest.raises(UnsupportedMediaTypeError):
        sanitize_filename("invoice\u202efdp.exe")


@pytest.mark.parametrize("raw", [None, "", "   ", "/", "C:\\", "\u202e\u200b"])
def test_missing_filename(raw: str | None) -> None:
    with pytest.raises(BadRequestError):
        sanitize_filename(raw)


def test_extension_only_filename_rejected() -> None:
    with pytest.raises(BadRequestError):
        sanitize_filename(".pdf")


def test_long_filename_truncated_keeping_extension() -> None:
    name = sanitize_filename("a" * 400 + ".PDF")
    assert len(name) == MAX_FILENAME_LENGTH
    assert name.endswith(".PDF")


# --- declared content type --------------------------------------------------------------


@pytest.mark.parametrize(
    "content_type",
    ["application/pdf", "APPLICATION/PDF", "application/pdf; charset=binary", "application/octet-stream", "", None],
)
def test_accepted_content_types(content_type: str | None) -> None:
    check_declared_content_type(content_type)


@pytest.mark.parametrize(
    "content_type",
    ["text/plain", "application/zip", "application/x-msdownload", "image/png", "text/html", "application/x-pdf-ish"],
)
def test_incompatible_content_types_rejected(content_type: str) -> None:
    with pytest.raises(UnsupportedMediaTypeError):
        check_declared_content_type(content_type)


# --- signature ----------------------------------------------------------------------------


@pytest.mark.parametrize("header", [b"%PDF-1.0", b"%PDF-1.7", b"%PDF-2.0"])
def test_valid_pdf_headers(header: bytes) -> None:
    require_pdf_signature(header)


@pytest.mark.parametrize(
    "header",
    [
        b"",
        b"%PDF",
        b"%PDF-",
        b"%pdf-1.7",          # case matters
        b"%PDF-x.y",
        b"PK\x03\x04\x14\x00\x06\x00",  # ZIP / DOCX
        b"MZ\x90\x00\x03\x00\x00\x00",  # EXE
        b"<html><b",
        b"\xef\xbb\xbf%PDF-",  # UTF-8 BOM before header
        b"   %PDF-1.7",        # leading bytes: rejected on purpose (strict byte-0 policy)
    ],
)
def test_invalid_pdf_headers(header: bytes) -> None:
    with pytest.raises(BadRequestError):
        require_pdf_signature(header)
