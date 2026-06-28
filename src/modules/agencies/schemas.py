"""Agencies module — request/response DTOs for `/agencies`.

Endpoints:
  GET    /agencies                     — list all agencies (SUPER_ADMIN, paginated)
  POST   /agencies                     — create one (SUPER_ADMIN)
  GET    /agencies/{agency_id}         — fetch one (SUPER_ADMIN)
  PATCH  /agencies/{agency_id}         — partial update (SUPER_ADMIN)
  DELETE /agencies/{agency_id}         — soft-delete (SUPER_ADMIN)
  GET    /agencies/{agency_id}/programs — list programs the agency offers (SUPER_ADMIN)

State machine: an agency moves through ACTIVE → TRIAL → SUSPENDED → CHURNED.
Soft-delete is a separate operation (sets `deleted_at`); the row stays
referencable by FK but is hidden from default reads.

`settings` is a free-form JSONB column for agency-level config that
doesn't warrant its own column (e.g. feature flags, custom branding).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.shared.domain.enums import AgencyStatus, ProgramType
from src.shared.schemas.pagination import PaginatedResponse


# --------------------------------------------------------------------------
# Base + Create
# --------------------------------------------------------------------------
class AgencyCreateRequest(BaseModel):
    """Body for POST /agencies.

    `name` is required; everything else defaults to the seed values.
    """

    name: str = Field(min_length=1, max_length=255)
    timezone: str = Field(default="America/Chicago", min_length=1, max_length=64)
    settings: dict[str, Any] = Field(default_factory=dict)
    initial_program_codes: list[str] = Field(
        default_factory=list,
        description=(
            "Optional list of ProgramType values to enable at creation "
            "(e.g. ['PCA', 'ARMHS']). Unknown codes return 422."
        ),
    )

    @field_validator("name", "timezone")
    @classmethod
    def _strip_non_empty(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("must not be empty or whitespace-only")
        return stripped

    @field_validator("initial_program_codes")
    @classmethod
    def _validate_program_codes(cls, v: list[str]) -> list[str]:
        valid = {pt.value for pt in ProgramType}
        unknown = [c for c in v if c not in valid]
        if unknown:
            raise ValueError(f"unknown program codes: {unknown}. valid: {sorted(valid)}")
        # Dedupe + preserve order
        seen: set[str] = set()
        out: list[str] = []
        for c in v:
            if c not in seen:
                seen.add(c)
                out.append(c)
        return out


# --------------------------------------------------------------------------
# Update (all fields optional; only set fields are written)
# --------------------------------------------------------------------------
class AgencyUpdateRequest(BaseModel):
    """Body for PATCH /agencies/{id}. All fields optional."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    status: AgencyStatus | None = Field(default=None)
    settings: dict[str, Any] | None = Field(default=None)

    @field_validator("name", "timezone")
    @classmethod
    def _strip_non_empty(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            raise ValueError("must not be empty or whitespace-only")
        return stripped


# --------------------------------------------------------------------------
# Response
# --------------------------------------------------------------------------
class AgencyResponse(BaseModel):
    """One agency — shape returned by GET / POST / PATCH."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    status: AgencyStatus
    timezone: str
    settings: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class AgencyListResponse(PaginatedResponse[AgencyResponse]):
    """Paginated list envelope."""

    pass


# --------------------------------------------------------------------------
# Programs
# --------------------------------------------------------------------------
class AgencyProgramResponse(BaseModel):
    """One (agency_id, program_id, is_enabled) triple with program details."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    program_id: UUID
    program_code: str
    program_name: str
    is_enabled: bool
    created_at: datetime


class AgencyProgramListResponse(BaseModel):
    """List of programs an agency offers (not paginated — bounded by `programs`)."""

    data: list[AgencyProgramResponse]


__all__ = [
    "AgencyCreateRequest",
    "AgencyListResponse",
    "AgencyProgramListResponse",
    "AgencyProgramResponse",
    "AgencyResponse",
    "AgencyUpdateRequest",
]
