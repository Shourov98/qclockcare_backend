"""Locations module — request/response DTOs for `/locations`.

Endpoints:
  GET    /locations                       — list agency's locations (paginated)
  POST   /locations                       — create (AGENCY_ADMIN)
  GET    /locations/{id}                  — fetch one
  PATCH  /locations/{id}                  — update (AGENCY_ADMIN)
  DELETE /locations/{id}                  — soft delete (AGENCY_ADMIN)

State machine: a location is "active" by default. AGENCY_ADMIN can
flip `is_active=false` to retire a location; soft delete removes the
row from default reads but preserves history (appointments still
reference it via FK ON DELETE SET NULL... actually, no — FK is
ON DELETE SET NULL per schema doc, so soft delete via the service
layer is preferred to preserve referential integrity).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.shared.schemas.pagination import PaginatedResponse


class LocationBase(BaseModel):
    """Shared field set used by Create + Update."""

    label: str | None = Field(default=None, max_length=120)
    address_line1: str = Field(min_length=1, max_length=200)
    address_line2: str | None = Field(default=None, max_length=200)
    city: str = Field(min_length=1, max_length=120)
    state: str = Field(min_length=2, max_length=2)
    postal_code: str = Field(min_length=1, max_length=20)
    country: str = Field(default="US", min_length=2, max_length=2)
    latitude: Decimal | None = Field(default=None)
    longitude: Decimal | None = Field(default=None)
    geofence_radius_m: int = Field(default=150, ge=10, le=5000)
    is_active: bool = True

    @field_validator("state")
    @classmethod
    def _state_upper(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("postal_code", "city", "address_line1")
    @classmethod
    def _strip_non_empty(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("must not be empty or whitespace-only")
        return stripped


class LocationCreateRequest(LocationBase):
    """Body for POST /locations."""

    # All fields live on the base; no additional inputs at create time.
    pass


class LocationUpdateRequest(BaseModel):
    """Body for PATCH /locations/{id}. All fields optional."""

    label: str | None = Field(default=None, max_length=120)
    address_line1: str | None = Field(default=None, min_length=1, max_length=200)
    address_line2: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, min_length=1, max_length=120)
    state: str | None = Field(default=None, min_length=2, max_length=2)
    postal_code: str | None = Field(default=None, min_length=1, max_length=20)
    country: str | None = Field(default=None, min_length=2, max_length=2)
    latitude: Decimal | None = Field(default=None)
    longitude: Decimal | None = Field(default=None)
    geofence_radius_m: int | None = Field(default=None, ge=10, le=5000)
    is_active: bool | None = Field(default=None)

    @field_validator("state")
    @classmethod
    def _state_upper(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return v.strip().upper()


class LocationResponse(BaseModel):
    """Single location — shape returned by GET / POST / PATCH."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    agency_id: UUID
    label: str | None
    address_line1: str
    address_line2: str | None
    city: str
    state: str
    postal_code: str
    country: str
    latitude: Decimal | None
    longitude: Decimal | None
    geofence_radius_m: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


class LocationListResponse(PaginatedResponse[LocationResponse]):
    """Cursor-paginated list envelope."""

    pass


__all__ = [
    "LocationCreateRequest",
    "LocationListResponse",
    "LocationResponse",
    "LocationUpdateRequest",
]
