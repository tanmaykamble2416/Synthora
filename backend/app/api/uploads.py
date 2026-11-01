"""Size-limited parsing of a single-file multipart upload.

The upload endpoint parses the body itself (instead of declaring a FastAPI File()
parameter) so that authentication and workspace ownership are checked BEFORE any
request body is read. This module caps how many body bytes are accepted:

  1. A declared Content-Length above the limit is rejected before reading anything.
  2. The ASGI receive channel is wrapped and counts the bytes actually received,
     so a missing or false Content-Length cannot bypass the limit.

The exact per-file limit is then enforced again while the file is copied into
storage (StorageService.stage).
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Request
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException

from app.core.exceptions import BadRequestError, PayloadTooLargeError, UnsupportedMediaTypeError

UPLOAD_FIELD = "file"
# Room for multipart boundaries and part headers on top of the file itself.
MULTIPART_OVERHEAD_BYTES = 64 * 1024

Receive = Callable[[], Awaitable[dict[str, Any]]]

UPLOAD_OPENAPI: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": [UPLOAD_FIELD],
                    "properties": {
                        UPLOAD_FIELD: {
                            "type": "string",
                            "contentMediaType": "application/octet-stream",
                            "description": "The PDF file (.pdf). Maximum size is set by MAX_UPLOAD_SIZE_MB.",
                        }
                    },
                }
            }
        },
    }
}


def too_large_message(max_file_bytes: int) -> str:
    return f"The upload exceeds the maximum size of {max_file_bytes // (1024 * 1024)} MB."


def _limited_receive(receive: Receive, limit: int, max_file_bytes: int) -> Receive:
    received = 0

    async def wrapped() -> dict[str, Any]:
        nonlocal received
        message = await receive()
        if message["type"] == "http.request":
            received += len(message.get("body", b""))
            if received > limit:
                raise PayloadTooLargeError(too_large_message(max_file_bytes))
        return message

    return wrapped


@asynccontextmanager
async def single_file_upload(request: Request, max_file_bytes: int) -> AsyncIterator[UploadFile]:
    """Yield the one uploaded file from a multipart body; always release spool files."""
    limit = max_file_bytes + MULTIPART_OVERHEAD_BYTES

    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            declared = int(declared_length)
        except ValueError:
            raise BadRequestError("Invalid Content-Length header.") from None
        if declared > limit:
            raise PayloadTooLargeError(too_large_message(max_file_bytes))

    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        raise UnsupportedMediaTypeError(f"Send the PDF as multipart/form-data in a '{UPLOAD_FIELD}' field.")

    limited = Request(request.scope, receive=_limited_receive(request.receive, limit, max_file_bytes))
    try:
        # One file, no extra text fields. Starlette closes its spool files if parsing fails.
        form = await limited.form(max_files=1, max_fields=0)
    except (MultiPartException, StarletteHTTPException) as exc:
        # Inside an app Starlette turns MultiPartException into HTTPException(400) carrying
        # parser details (e.g. configured limits); replace it with our own message.
        if isinstance(exc, StarletteHTTPException) and exc.status_code != 400:
            raise
        raise BadRequestError(f"Malformed upload. Send exactly one file in the '{UPLOAD_FIELD}' field.") from exc

    try:
        files = form.getlist(UPLOAD_FIELD)
        if len(form) != 1 or len(files) != 1 or not isinstance(files[0], UploadFile):
            raise BadRequestError(f"Send exactly one file in the '{UPLOAD_FIELD}' field.")
        yield files[0]
    finally:
        await form.close()
