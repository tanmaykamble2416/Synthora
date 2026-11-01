"""Validation of uploaded PDFs: filename, declared MIME type and file signature.

None of these checks trusts the client alone: the extension and MIME type only
filter out obviously wrong uploads; the %PDF- signature in the file content is
what actually decides.
"""

import re
import unicodedata

from app.core.exceptions import BadRequestError, UnsupportedMediaTypeError

MAX_FILENAME_LENGTH = 255  # papers.original_filename is VARCHAR(255)
PDF_EXTENSION = ".pdf"
PDF_HEADER_SIZE = 8  # enough for "%PDF-1.7"
_PDF_HEADER = re.compile(rb"%PDF-\d\.\d")

ACCEPTED_CONTENT_TYPE = "application/pdf"
# Browsers/clients sometimes send no type or a generic one; accepted only because
# the signature check below still has to pass.
GENERIC_CONTENT_TYPES = frozenset({"", "application/octet-stream"})

ONLY_PDF_MESSAGE = "Only PDF files (.pdf) are accepted."


def sanitize_filename(raw: str | None) -> str:
    """Return a safe display name. It is stored as metadata only, never used as a path."""
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]  # basename, incl. Windows paths
    name = unicodedata.normalize("NFC", name)
    # Drop control and format characters, e.g. U+202E RIGHT-TO-LEFT OVERRIDE, which can
    # make "invoice‮fdp.exe" display as "invoiceexe.pdf".
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C").strip()
    if not name:
        raise BadRequestError("The uploaded file must have a filename.")
    if not name.lower().endswith(PDF_EXTENSION):
        raise UnsupportedMediaTypeError(ONLY_PDF_MESSAGE)
    if len(name) == len(PDF_EXTENSION):
        raise BadRequestError("The filename must have a name before '.pdf'.")
    if len(name) > MAX_FILENAME_LENGTH:
        name = name[: MAX_FILENAME_LENGTH - len(PDF_EXTENSION)] + name[-len(PDF_EXTENSION):]
    return name


def check_declared_content_type(content_type: str | None) -> None:
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if media_type != ACCEPTED_CONTENT_TYPE and media_type not in GENERIC_CONTENT_TYPES:
        raise UnsupportedMediaTypeError(ONLY_PDF_MESSAGE)


def require_pdf_signature(header: bytes) -> None:
    """The file must start with a PDF header such as '%PDF-1.7'."""
    if not _PDF_HEADER.match(header):
        raise BadRequestError("The file is not a valid PDF (it does not start with a %PDF- header).")
