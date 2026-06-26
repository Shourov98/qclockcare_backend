"""Pagination schemas — offset (small lists) and cursor (large lists).

Per `15_PAGINATION_AND_FILTERING.md`:
- Offset: page_size ≤ 100, max page 10000. For staff/patients/appointments.
- Cursor: for unbounded lists (notifications, audit logs).
- Always stable secondary sort by `created_at` + `id`.

Cursor format: base64-encoded JSON of `{"created_at": "...", "id": "..."}`.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from math import ceil
from typing import Any, Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, Field

T = TypeVar("T")


# --------------------------------------------------------------------------
# Offset pagination (default)
# --------------------------------------------------------------------------
class OffsetPagination(BaseModel):
    """Offset-based pagination metadata."""

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    total: int = Field(default=0, ge=0)
    total_pages: int = Field(default=0, ge=0)


class PaginatedResponse(BaseModel, Generic[T]):
    """Response envelope for paginated lists.

    `data` is the list of items for the current page.
    `meta.pagination` carries offset-style pagination metadata.
    """

    data: list[T]
    pagination: OffsetPagination


def build_offset_response(
    items: list[T],
    *,
    total: int,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    """Build the standard offset-paginated response body."""
    total_pages = ceil(total / page_size) if page_size else 0
    return {
        "data": items,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        },
    }


# --------------------------------------------------------------------------
# Cursor pagination (for unbounded lists)
# --------------------------------------------------------------------------
class CursorPagination(BaseModel):
    """Cursor-based pagination metadata."""

    next_cursor: str | None = None
    has_more: bool = False
    limit: int = Field(default=20, ge=1, le=100)


def encode_cursor(*, created_at: datetime, id: UUID | str) -> str:
    """Encode a (timestamp, id) tuple into an opaque cursor token."""
    payload = {
        "created_at": created_at.isoformat(),
        "id": str(id),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Decode a cursor token back into (timestamp, id).

    Raises:
        ValueError: if the cursor is malformed.
    """
    try:
        # Re-pad if necessary
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw)
        created_at = datetime.fromisoformat(payload["created_at"])
        id_value = UUID(str(payload["id"]))
        return created_at, id_value
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid cursor: {exc}") from exc
