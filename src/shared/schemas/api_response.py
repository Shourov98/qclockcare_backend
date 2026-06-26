"""Standard API response envelope.

Successful responses use:
    { "data": <payload>, "meta": { "request_id": "...", "timestamp": "..." } }

Errors use the envelope in `src/core/exceptions.py`:
    { "error": { "code": "...", "message": "...", "request_id": "...", ... } }
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ApiMeta(BaseModel):
    """Metadata wrapper included in every successful response."""

    request_id: str = Field(default="")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ApiResponse(BaseModel, Generic[T]):
    """Successful response envelope.

    `data` is whatever the endpoint returns. `meta` carries the request ID
    and timestamp for tracing.
    """

    data: T
    meta: ApiMeta = Field(default_factory=ApiMeta)


def error_envelope(
    *,
    code: str,
    message: str,
    request_id: str = "",
    details: dict[str, Any] | list[Any] | None = None,
) -> dict[str, Any]:
    """Build an error response dict (mirrors the global handler's shape)."""
    body: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id,
            "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    }
    if details is not None:
        body["error"]["details"] = details
    return body
