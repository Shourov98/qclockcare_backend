"""Domain exception hierarchy + global handlers.

Every business error inherits from `AppException`. Routes never `try/except`
for control flow; services raise typed exceptions and the global handler
maps them to the standard error envelope (`18_ERROR_MAPPING.md`).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Base exception
# --------------------------------------------------------------------------
class AppException(Exception):
    """Base for all domain exceptions.

    Subclasses set `http_status` and `error_code`. The default message can be
    overridden per instance via the `message` kwarg.
    """

    http_status: int = 500
    error_code: str = "INTERNAL_ERROR"
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        self.details: dict[str, Any] = details or {}
        super().__init__(self.message)


# --------------------------------------------------------------------------
# Common subclasses — shared across modules
# --------------------------------------------------------------------------
class NotFoundError(AppException):
    http_status = 404
    error_code = "NOT_FOUND"
    message = "Resource not found."


class ValidationError(AppException):
    http_status = 422
    error_code = "VALIDATION_ERROR"
    message = "Request failed validation."


class UnauthorizedError(AppException):
    http_status = 401
    error_code = "UNAUTHORIZED"
    message = "Authentication required."


class TokenExpiredError(AppException):
    http_status = 401
    error_code = "TOKEN_EXPIRED"
    message = "Access token has expired."


class TokenInvalidError(AppException):
    http_status = 401
    error_code = "TOKEN_INVALID"
    message = "Token signature is invalid."


class ForbiddenError(AppException):
    http_status = 403
    error_code = "FORBIDDEN"
    message = "Action not permitted."


class InsufficientPermissionsError(ForbiddenError):
    error_code = "INSUFFICIENT_PERMISSIONS"
    message = "Your role does not permit this action."


class CrossAgencyAccessDeniedError(ForbiddenError):
    error_code = "CROSS_AGENCY_ACCESS_DENIED"
    message = "You cannot access another agency's resources."


class ConflictError(AppException):
    http_status = 409
    error_code = "CONFLICT"
    message = "Resource state conflict."


class DuplicateResourceError(ConflictError):
    error_code = "DUPLICATE_RESOURCE"
    message = "Resource already exists."


class InvalidStateTransitionError(ConflictError):
    error_code = "INVALID_STATE_TRANSITION"
    message = "The requested state transition is not allowed."


class RateLimitExceededError(AppException):
    http_status = 429
    error_code = "RATE_LIMIT_EXCEEDED"
    message = "Too many requests."


class ServiceUnavailableError(AppException):
    http_status = 503
    error_code = "SERVICE_UNAVAILABLE"
    message = "An external dependency is unavailable."


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _envelope(
    *,
    code: str,
    message: str,
    request_id: str,
    details: dict[str, Any] | list[Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id,
            "timestamp": _now_iso(),
        }
    }
    if details is not None:
        body["error"]["details"] = details
    return body


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------
async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "")
    body = _envelope(
        code=exc.error_code,
        message=exc.message,
        request_id=request_id,
        details=exc.details or None,
    )
    # 5xx are surprises → log with stack trace. 4xx are expected → log a warning.
    if exc.http_status >= 500:
        logger.exception(
            "Unhandled app exception",
            extra={"request_id": request_id, "code": exc.error_code},
        )
    else:
        logger.warning(
            "App exception: %s — %s",
            exc.error_code,
            exc.message,
            extra={"request_id": request_id, "status": exc.http_status},
        )
    headers = {"X-Request-ID": request_id} if request_id else None
    return JSONResponse(status_code=exc.http_status, content=body, headers=headers)


async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Pydantic validation errors → 422 with field-level details list."""
    request_id = getattr(request.state, "request_id", "")
    details = [
        {
            "field": ".".join(str(part) for part in err["loc"]),
            "message": err["msg"],
            "type": err["type"],
        }
        for err in exc.errors()
    ]
    body = _envelope(
        code="VALIDATION_ERROR",
        message="Request body failed validation.",
        request_id=request_id,
        details=details,
    )
    headers = {"X-Request-ID": request_id} if request_id else None
    return JSONResponse(status_code=422, content=body, headers=headers)


async def http_exception_handler(
    request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    """Handle non-AppException HTTP errors (404 from router, 405, etc.)."""
    request_id = getattr(request.state, "request_id", "")
    code = _code_for_status(exc.status_code)
    body = _envelope(
        code=code,
        message=str(exc.detail) if exc.detail else _default_message(exc.status_code),
        request_id=request_id,
    )
    headers = {"X-Request-ID": request_id} if request_id else None
    return JSONResponse(status_code=exc.status_code, content=body, headers=headers)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all for unanticipated exceptions → 500 with a generic message."""
    request_id = getattr(request.state, "request_id", "")
    logger.exception(
        "Unhandled exception",
        extra={"request_id": request_id},
    )
    body = _envelope(
        code="INTERNAL_ERROR",
        message="An unexpected error occurred.",
        request_id=request_id,
    )
    headers = {"X-Request-ID": request_id} if request_id else None
    return JSONResponse(status_code=500, content=body, headers=headers)


# --------------------------------------------------------------------------
# Registration helper
# --------------------------------------------------------------------------
def _code_for_status(status_code: int) -> str:
    return {
        400: "BAD_REQUEST",
        401: "UNAUTHORIZED",
        403: "FORBIDDEN",
        404: "NOT_FOUND",
        405: "METHOD_NOT_ALLOWED",
        409: "CONFLICT",
        413: "PAYLOAD_TOO_LARGE",
        415: "UNSUPPORTED_MEDIA_TYPE",
        422: "VALIDATION_ERROR",
        429: "RATE_LIMIT_EXCEEDED",
        500: "INTERNAL_ERROR",
        503: "SERVICE_UNAVAILABLE",
    }.get(status_code, "INTERNAL_ERROR")


def _default_message(status_code: int) -> str:
    return {
        400: "Bad request.",
        401: "Authentication required.",
        403: "Action not permitted.",
        404: "Resource not found.",
        405: "Method not allowed.",
        409: "Conflict.",
        422: "Request failed validation.",
        429: "Too many requests.",
        500: "An unexpected error occurred.",
        503: "Service unavailable.",
    }.get(status_code, "Error.")


def register_exception_handlers(app: FastAPI) -> None:
    """Wire all global handlers onto the FastAPI app.

    Order matters: register specific handlers before the catch-all.
    """
    # The FastAPI/Starlette handler signature is `(Request, Exception) -> Response`,
    # but our handlers take more specific exception subtypes. The cast here is
    # safe because we register the handler with the matching exception class.
    app.add_exception_handler(AppException, app_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)


__all__ = [
    "AppException",
    "ConflictError",
    "CrossAgencyAccessDeniedError",
    "DuplicateResourceError",
    "ForbiddenError",
    "InsufficientPermissionsError",
    "InvalidStateTransitionError",
    "NotFoundError",
    "RateLimitExceededError",
    "ServiceUnavailableError",
    "TokenExpiredError",
    "TokenInvalidError",
    "UnauthorizedError",
    "ValidationError",
    "register_exception_handlers",
]
