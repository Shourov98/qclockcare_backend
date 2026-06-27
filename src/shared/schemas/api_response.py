"""Standard API response envelope.

Successful responses use:
    { "data": <payload>, "meta": { "request_id": "...", "timestamp": "..." } }

Errors use the envelope in `src/shared/schemas/error.py`:
    { "error": { "code": "...", "message": "...", "request_id": "...", ... } }

`error_envelope(...)` is kept as a thin dict-returning wrapper for
backward compatibility with callers that want a `dict` (e.g. in
tests). The typed `ErrorResponse` model in `src/shared/schemas/error.py`
is the source of truth — the global handler in
`src/core/exceptions.py` and this helper both delegate to it.
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
    """Build an error response dict.

    Delegates to the typed `ErrorResponse` model so this helper and
    the runtime handler in `src/core/exceptions.py` can never
    disagree on shape.
    """
    # Local import keeps the import graph shallow for callers that
    # only need the success-path `ApiResponse` / `ApiMeta`.
    from src.shared.schemas.error import build_error_envelope

    return build_error_envelope(
        code=code,
        message=message,
        request_id=request_id,
        details=details,
    ).model_dump(mode="json", exclude_none=True)
