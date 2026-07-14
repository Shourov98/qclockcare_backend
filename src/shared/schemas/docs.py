"""OpenAPI documentation scaffold.

Single import point for everything `/docs` and `/openapi.json`
need from a route or from the FastAPI constructor:

    from src.shared.schemas.docs import (
        OPENAPI_SECURITY_SCHEMES,
        OPENAPI_SECURITY,
        STANDARD_ERROR_RESPONSES,
        tags_metadata,
        standard_responses,
    )

The `FastAPI(...)` constructor in `src/main.py` consumes
`tags_metadata`, `OPENAPI_SECURITY_SCHEMES`, and `OPENAPI_SECURITY`.
Each router decorator consumes `standard_responses(include=[...])`
to attach pre-wired `401` / `403` / `404` / `409` / `422` examples
to its routes — no per-route boilerplate.

Per-field examples and descriptions live on the Pydantic schemas
themselves (via `model_config = ConfigDict(json_schema_extra=...)`
and `Field(description=...)`). This module just exposes the
scaffolding so every route can opt-in cheaply.
"""

from __future__ import annotations

from typing import Any, Final

from src.shared.schemas.error import ErrorBody, ErrorDetail, ErrorResponse

# --------------------------------------------------------------------------
# OpenAPI security scheme
# --------------------------------------------------------------------------
OPENAPI_SECURITY_SCHEMES: Final[dict[str, dict[str, Any]]] = {
    "HTTPBearer": {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": (
            "JWT access token issued by `POST /auth/login`. "
            "Send as `Authorization: Bearer <access_token>`. "
            "Refresh with `POST /auth/refresh`; the access token "
            "lifetime is `settings.ACCESS_TOKEN_EXPIRY_MINUTES`."
        ),
    }
}

# Apply globally — every endpoint expects Bearer auth unless its
# decorator overrides `security=[]`. The actual auth check happens
# via the dependency chain (`get_session_with_auth` /
# `get_current_auth`); the `security` block is purely for Swagger
# UI's "Authorize" button.
OPENAPI_SECURITY: Final[list[dict[str, list[str]]]] = [
    {"HTTPBearer": []}
]


# --------------------------------------------------------------------------
# Tag metadata — drives the sidebar in /docs and /redoc
# --------------------------------------------------------------------------
tags_metadata: Final[list[dict[str, str]]] = [
    {
        "name": "auth",
        "description": (
            "Authentication flows — login, refresh, logout, "
            "invitation acceptance, OTP verification, password reset, "
            "and `GET /auth/me`. All `/auth/*` endpoints are public "
            "except `POST /auth/logout` and `GET /auth/me` which "
            "require a valid bearer token."
        ),
    },
    {
        "name": "staff",
        "description": (
            "Care-staff profiles, qualifications (PCA, CPR, RN, "
            "etc.), and weekly availability windows. Most write "
            "operations require `AGENCY_ADMIN`. Read operations are "
            "open to `AGENCY_ADMIN`, `STAFF` (own profile only), "
            "and `SUPER_ADMIN`."
        ),
    },
    {
        "name": "patients",
        "description": (
            "Patient profiles, guardian profiles, and the "
            "patient<->guardian relationship graph. Includes the "
            "patient-side `qualifications`-style endpoints for "
            "guardian invitations. Write operations require "
            "`AGENCY_ADMIN`; patients and guardians see their own "
            "data via the `/portal/*` namespace."
        ),
    },
    {
        "name": "appointments",
        "description": (
            "Appointment lifecycle — creation, assignment, status "
            "transitions, confirmation, reschedule / cancellation "
            "requests, service items, and the immutable "
            "appointment-event timeline."
        ),
    },
    {
        "name": "visits",
        "description": (
            "Visit execution — check-in / check-out, status "
            "transitions, per-service-item verification, visit "
            "notes, and the issue / dispute workflow. Visits are "
            "the operational counterpart to scheduled appointments."
        ),
    },
    {
        "name": "portal",
        "description": (
            "Patient / guardian-facing read-only views + actions. "
            "All endpoints here require a `PATIENT` or `GUARDIAN` "
            "bearer token and return data scoped to the caller's "
            "linked patients."
        ),
    },
    {
        "name": "notifications",
        "description": (
            "In-app notifications, read-state, per-channel "
            "preferences, the unread-badge endpoint, and the "
            "admin broadcast endpoint (`POST /notifications/broadcast`)."
        ),
    },
    {
        "name": "locations",
        "description": (
            "Agency-scoped service locations (home addresses, "
            "clinic rooms, etc.) used by appointments and visits."
        ),
    },
    {
        "name": "audit-logs",
        "description": (
            "Immutable security + compliance audit trail. Read-only. "
            "Filterable by actor, entity, action, and date range. "
            "Requires `AGENCY_ADMIN` or `SUPER_ADMIN`."
        ),
    },
    {
        "name": "agencies",
        "description": (
            "Agency-tenant management — create, list, suspend, and "
            "soft-delete agencies, plus list the programs each agency "
            "offers. **SUPER_ADMIN only.** AGENCY_ADMIN does not have "
            "an agency-management surface (their agency is managed for them)."
        ),
    },
    {
        "name": "health",
        "description": (
            "Liveness (`/health`) and readiness (`/ready`) probes. "
            "Public — no auth required."
        ),
    },
]


# --------------------------------------------------------------------------
# Standard error responses — examples keyed by error code
# --------------------------------------------------------------------------
_EXAMPLE_UNAUTHORIZED: Final[dict[str, Any]] = {
    "error": {
        "code": "UNAUTHORIZED",
        "message": "Authentication required.",
        "request_id": "5f3a7b1c-1d0a-4a23-9c8e-1b2c3d4e5f6a",
        "timestamp": "2026-06-28T10:23:01Z",
    }
}

_EXAMPLE_FORBIDDEN: Final[dict[str, Any]] = {
    "error": {
        "code": "INSUFFICIENT_PERMISSIONS",
        "message": "Your role does not permit this action.",
        "request_id": "5f3a7b1c-1d0a-4a23-9c8e-1b2c3d4e5f6a",
        "timestamp": "2026-06-28T10:23:01Z",
        "details": {"required_any_of": ["AGENCY_ADMIN"]},
    }
}

_EXAMPLE_NOT_FOUND: Final[dict[str, Any]] = {
    "error": {
        "code": "NOT_FOUND",
        "message": "Resource not found.",
        "request_id": "5f3a7b1c-1d0a-4a23-9c8e-1b2c3d4e5f6a",
        "timestamp": "2026-06-28T10:23:01Z",
    }
}

_EXAMPLE_CONFLICT: Final[dict[str, Any]] = {
    "error": {
        "code": "DUPLICATE_RESOURCE",
        "message": "A record with these unique fields already exists.",
        "request_id": "5f3a7b1c-1d0a-4a23-9c8e-1b2c3d4e5f6a",
        "timestamp": "2026-06-28T10:23:01Z",
    }
}

_EXAMPLE_VALIDATION: Final[dict[str, Any]] = {
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

# Map HTTP status code (as string, FastAPI's convention) ->
# (description, example, error_code).
STANDARD_ERROR_RESPONSES: Final[dict[str, dict[str, Any]]] = {
    "401": {
        "model": ErrorResponse,
        "description": (
            "Authentication required or token invalid / expired. "
            "Body uses the standard error envelope with `code` = "
            "`UNAUTHORIZED` (or a more specific variant like "
            "`TOKEN_EXPIRED`, `INVALID_CREDENTIALS`, "
            "`ACCOUNT_LOCKED`)."
        ),
        "content": {
            "application/json": {
                "schema": ErrorResponse.model_json_schema(),
                "example": _EXAMPLE_UNAUTHORIZED,
            }
        },
    },
    "403": {
        "model": ErrorResponse,
        "description": (
            "Authenticated but not permitted. `code` is typically "
            "`INSUFFICIENT_PERMISSIONS` (your role is wrong), "
            "`CROSS_AGENCY_ACCESS_DENIED` (you're scoped to a "
            "different agency), `EMAIL_NOT_VERIFIED`, or "
            "`ACCOUNT_DISABLED`."
        ),
        "content": {
            "application/json": {
                "schema": ErrorResponse.model_json_schema(),
                "example": _EXAMPLE_FORBIDDEN,
            }
        },
    },
    "404": {
        "model": ErrorResponse,
        "description": (
            "Resource not found. The ID is well-formed but no "
            "matching row exists (or it belongs to a different "
            "agency and RLS hid it)."
        ),
        "content": {
            "application/json": {
                "schema": ErrorResponse.model_json_schema(),
                "example": _EXAMPLE_NOT_FOUND,
            }
        },
    },
    "409": {
        "model": ErrorResponse,
        "description": (
            "Conflict. The request is well-formed but cannot be "
            "applied because of current state — e.g. "
            "`DUPLICATE_RESOURCE`, `INVALID_STATE_TRANSITION`, "
            "`INVITATION_ALREADY_CONSUMED`."
        ),
        "content": {
            "application/json": {
                "schema": ErrorResponse.model_json_schema(),
                "example": _EXAMPLE_CONFLICT,
            }
        },
    },
    "422": {
        "model": ErrorResponse,
        "description": (
            "Request body failed Pydantic validation. `details` is "
            "a list of `{field, message, type}` entries (one per "
            "failing field)."
        ),
        "content": {
            "application/json": {
                "schema": ErrorResponse.model_json_schema(),
                "example": _EXAMPLE_VALIDATION,
            }
        },
    },
}


def standard_responses(
    include: list[int] | None = None,
    *,
    extras: dict[int | str, dict[str, Any]] | None = None,
) -> dict[int | str, dict[str, Any]]:
    """Return a FastAPI `responses=` dict pre-wired with error examples.

    Usage on a route decorator:

        @router.post(
            "/staff",
            response_model=StaffProfileResponse,
            responses=standard_responses(include=[401, 403, 409, 422]),
            summary="Invite a new staff member",
        )

    Parameters:
        include: which of the standard codes (`401`, `403`, `404`,
            `409`, `422`) to attach. Default is all five.
        extras: extra response entries keyed by HTTP status code
            (int) or arbitrary string. Useful for success-path
            examples (`200`, `201`) on routes that don't have a
            `response_model=...` or where you want to override
            the default schema with a richer example.

    Returns:
        A dict suitable for FastAPI's `responses=` decorator arg.
        Keys are HTTP status codes (as `str` for the standard
        entries, `int` for the extras — FastAPI normalises both).
    """
    selected = include if include is not None else [401, 403, 404, 409, 422]
    out: dict[int | str, dict[str, Any]] = {
        str(code): STANDARD_ERROR_RESPONSES[str(code)] for code in selected
    }
    if extras:
        out.update(extras)
    return out


__all__ = [
    "OPENAPI_SECURITY",
    "OPENAPI_SECURITY_SCHEMES",
    "STANDARD_ERROR_RESPONSES",
    "ErrorBody",
    "ErrorDetail",
    "ErrorResponse",
    "standard_responses",
    "tags_metadata",
]
