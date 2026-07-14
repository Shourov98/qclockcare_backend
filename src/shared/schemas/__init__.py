"""Shared Pydantic schemas — response envelope, error envelope, pagination."""

from src.shared.schemas.api_response import ApiMeta, ApiResponse, error_envelope
from src.shared.schemas.error import (
    ErrorBody,
    ErrorDetail,
    ErrorResponse,
    build_error_envelope,
)
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
    "ErrorBody",
    "ErrorDetail",
    "ErrorResponse",
    "OffsetPagination",
    "PaginatedResponse",
    "build_error_envelope",
    "decode_cursor",
    "encode_cursor",
    "error_envelope",
]
