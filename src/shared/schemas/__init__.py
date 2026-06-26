"""Shared Pydantic schemas — response envelope and pagination."""

from src.shared.schemas.api_response import ApiMeta, ApiResponse, error_envelope
from src.shared.schemas.pagination import (
    CursorPagination,
    OffsetPagination,
    PaginatedResponse,
    decode_cursor,
    encode_cursor,
)

__all__ = [
    "ApiMeta",
    "ApiResponse",
    "CursorPagination",
    "OffsetPagination",
    "PaginatedResponse",
    "decode_cursor",
    "encode_cursor",
    "error_envelope",
]
