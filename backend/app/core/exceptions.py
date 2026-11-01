"""Domain exceptions and global handlers that produce one consistent error shape:

    {"error": {"code": "...", "message": "...", "details": [...] | null}}
"""

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.schemas.error import ErrorBody, ErrorResponse

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Base class for errors the API is allowed to show to clients."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "INTERNAL_ERROR"
    headers: dict[str, str] | None = None

    def __init__(self, message: str, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class DatabaseUnavailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "DATABASE_UNAVAILABLE"


class ExtensionMissingError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "DATABASE_EXTENSION_MISSING"


class AuthenticationError(AppError):
    """Missing/invalid/expired token or bad login. Message is intentionally generic."""

    status_code = status.HTTP_401_UNAUTHORIZED
    code = "AUTHENTICATION_FAILED"
    headers = {"WWW-Authenticate": "Bearer"}


class BadRequestError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "BAD_REQUEST"


class NotFoundError(AppError):
    """Also used for resources owned by someone else, so their existence is not revealed."""

    status_code = status.HTTP_404_NOT_FOUND
    code = "NOT_FOUND"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "CONFLICT"


class PayloadTooLargeError(AppError):
    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    code = "PAYLOAD_TOO_LARGE"


class UnsupportedMediaTypeError(AppError):
    status_code = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    code = "UNSUPPORTED_MEDIA_TYPE"


def _error_response(
    status_code: int,
    code: str,
    message: str,
    details: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(code=code, message=message, details=details))
    return JSONResponse(status_code=status_code, content=jsonable_encoder(body), headers=headers)


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    logger.warning("%s on %s %s: %s", exc.code, request.method, request.url.path, exc.message)
    return _error_response(exc.status_code, exc.code, exc.message, exc.details, exc.headers)


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Only loc/msg/type are returned: the rejected *input* is never echoed back,
    # so a password that fails validation does not appear in the response.
    details = [
        {"loc": list(err.get("loc", ())), "msg": err.get("msg", ""), "type": err.get("type", "")}
        for err in exc.errors()
    ]
    return _error_response(
        status.HTTP_422_UNPROCESSABLE_CONTENT, "VALIDATION_ERROR", "Request validation failed.", details
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    message = exc.detail if isinstance(exc.detail, str) else "HTTP error."
    headers = getattr(exc, "headers", None)
    return _error_response(exc.status_code, f"HTTP_{exc.status_code}", message, headers=headers)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Full traceback goes to the server log; the client never sees internals.
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return _error_response(
        status.HTTP_500_INTERNAL_SERVER_ERROR, "INTERNAL_ERROR", "An unexpected error occurred."
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
