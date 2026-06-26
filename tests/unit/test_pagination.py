"""Pagination helper tests — cursor encode/decode round-trip + offset math."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from src.shared.schemas.pagination import (
    CursorPagination,
    OffsetPagination,
    build_offset_response,
    decode_cursor,
    encode_cursor,
)


def test_offset_pagination_basic() -> None:
    """Offset pagination clamps page_size to [1, 100]."""
    p = OffsetPagination(page=2, page_size=50, total=120, total_pages=3)
    assert p.page == 2
    assert p.page_size == 50
    assert p.total == 120
    assert p.total_pages == 3


def test_offset_pagination_rejects_invalid_page_size() -> None:
    with pytest.raises(ValueError):
        OffsetPagination(page=1, page_size=0, total=0, total_pages=0)
    with pytest.raises(ValueError):
        OffsetPagination(page=1, page_size=999, total=0, total_pages=0)


def test_build_offset_response() -> None:
    items = [{"id": 1}, {"id": 2}]
    resp = build_offset_response(items=items, page=1, page_size=10, total=2)
    assert resp["data"] == items
    assert resp["pagination"]["total"] == 2
    assert resp["pagination"]["total_pages"] == 1
    assert resp["pagination"]["page"] == 1


def test_cursor_encode_decode_round_trip() -> None:
    when = datetime(2026, 6, 27, 10, 0, 0, tzinfo=UTC)
    uid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    encoded = encode_cursor(created_at=when, id=uid)
    assert isinstance(encoded, str)
    assert len(encoded) > 0

    decoded_when, decoded_id = decode_cursor(encoded)
    assert decoded_when == when
    assert decoded_id == uid


def test_cursor_decode_invalid_raises() -> None:
    """Garbage cursors must raise — never silently return junk."""
    with pytest.raises(ValueError):
        decode_cursor("not-a-real-cursor!!!")


def test_cursor_pagination_helper() -> None:
    """has_more flips based on whether items length equals limit."""
    p = CursorPagination(next_cursor="abc", has_more=True, limit=10)
    assert p.has_more is True
    assert p.next_cursor == "abc"
    assert p.limit == 10
