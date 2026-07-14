"""Project-wide error envelope — single source of truth.

Every error response on the wire uses this shape (see
`src/core/exceptions.py:app_exception_handler` for the runtime
producer). Centralising the model in Pydantic means:

  - The OpenAPI schema (`/openapi.json`) and the runtime JSON
    output cannot drift — both are produced from the same model.
  - Service / test code can assert against the typed model
    rather than a hand-built dict.
  - The `details` field has a defined shape (list of `ErrorDetail`
    for validation, free-form dict for typed `AppException`s).

Wire shape (422 example):
    {
      "error": {
        "code": "VALIDATION_ERROR",
        "message": "Request body failed validation.",
        "request_id": "5f3a-...",
        "timestamp": "2026-06-28T10:23:01Z",
        "details": [
          {"field": "email", "message": "value is not a valid email address",
           "type": "value_error.email"}
        ]
      }
    }

The two legacy dict-builders (`_envelope()` in core/exceptions.py
and `error_envelope()` in shared/schemas/api_response.py) both
delegate to `build_error_envelope(...)` here — they keep their
function signatures for backward compatibility but emit identical
JSON.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorDetail(BaseModel):
    """Per-field or per-rule error detail.

    Used in two places:

    - 422 `VALIDATION_ERROR` — one entry per failing field, with
      the dotted `field` path (e.g. `"address.zip_code"`),
      a human-readable `message`, and the Pydantic `type` tag
      (e.g. `"value_error.email"`).
    - Typed `AppException.details` payloads — free-form key/value
      pairs (e.g. `{"required_any_of": ["AGENCY_ADMIN"]}`).

    `field` is optional because some errors aren't tied to a
    specific field (e.g. a 409 INVITATION_ALREADY_CONSUMED on a
    whole resource).
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "field": "email",
                    "message": "value is not a valid email address",
                    "type": "value_error.email",
                }
            ]
        }
    )

    field: str | None = Field(
        default=None,
        description=(
            "Dotted path to the failing field (e.g. `address.zip_code`). "
            "`null` for resource-level errors."
        ),
    )
    message: str = Field(description="Human-readable explanation of the failure.")
    type: str | None = Field(
        default=None,
        description=(
            "Pydantic error type tag (e.g. `value_error.email`, "
            "`type_error.integer`). Useful for clients that want to "
            "branch on the failure mode without parsing the message."
        ),
    )


class ErrorBody(BaseModel):
    """Inner body of the error envelope."""

    code: str = Field(
        description=(
            "Machine-readable error code (e.g. `UNAUTHORIZED`, "
            "`VALIDATION_ERROR`, `INSUFFICIENT_PERMISSIONS`). "
            "Stable across releases — clients should branch on this."
        ),
    )
    message: str = Field(
        description="Human-readable summary of the failure.",
    )
    request_id: str = Field(
        default="",
        description=(
            "Echoes the `X-Request-ID` header from the request. "
            "Include this in support tickets so we can grep our logs."
        ),
    )
    timestamp: datetime = Field(
        # Second-precision UTC ISO-8601 (no microseconds) to match
        # the on-the-wire format the project has always emitted —
        # `_now_iso()` in src/core/exceptions.py:197 used
        # `strftime("%Y-%m-%dT%H:%M:%SZ")` and any existing clients
        # may be parsing the timestamp string.
        default_factory=lambda: datetime.now(UTC).replace(microsecond=0),
        description="UTC ISO-8601 timestamp of when the response was produced.",
    )
    details: list[ErrorDetail] | dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional context. For 422 validation errors this is a "
            "list of `ErrorDetail` (one per failing field). For typed "
            "`AppException`s this is a free-form dict (e.g. "
            "`{\"required_any_of\": [\"AGENCY_ADMIN\"]}`)."
        ),
    )


class ErrorResponse(BaseModel):
    """Project-wide error envelope.

    Every non-2xx response uses this shape. The schema in
    `/openapi.json` references this model so Swagger UI shows
    the same JSON example for every documented error code.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "error": {
                        "code": "VALIDATION_ERROR",
                        "message": "Request body failed validation.",
                        "request_id": "5f3a7b1c-1d0a-4a23-9c8e-1b2c3d4e5f6a",
                        "timestamp": "2026-06-28T10:23:01Z",
                        "details": [
                            {
                                "field": "email",
                                "message": "value is not a valid email address",
                                "type": "value_error.email",
                            }
                        ],
                    }
                }
            ]
        }
    )

    error: ErrorBody


def build_error_envelope(
    *,
    code: str,
    message: str,
    request_id: str = "",
    details: list[ErrorDetail] | dict[str, Any] | None = None,
) -> ErrorResponse:
    """Build a typed `ErrorResponse` — the single source of truth.

    Both `core/exceptions.py:_envelope` and
    `shared/schemas/api_response.py:error_envelope` delegate here so
    the runtime JSON output and the OpenAPI schema share one model.
    """
    return ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            request_id=request_id,
            # Second-precision UTC ISO-8601 — matches the on-wire
            # format the project has always emitted.
            timestamp=datetime.now(UTC).replace(microsecond=0),
            details=details,
        )
    )


__all__ = [
    "ErrorBody",
    "ErrorDetail",
    "ErrorResponse",
    "build_error_envelope",
]
