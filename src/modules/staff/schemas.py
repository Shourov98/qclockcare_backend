"""Staff module — request/response Pydantic schemas (DTOs).

The wire format for every staff endpoint. Pattern:
- `*Request`  — what the client sends (validated, trimmed)
- `*Response` — what we return (excludes secrets / internal-only fields)
- Nested `*Nested` — child resources inlined in a parent response

All identifiers are UUIDs (validated as strings — clients may pass opaque
strings). Date / time fields are ISO-8601.

See `13_DATABASE_SCHEMA_COMPLETE.md` §6 for the data model.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, model_validator

from src.shared.domain.enums import (
    ProgramType,
    QualificationStatus,
    QualificationType,
    UserStatus,
)

# --------------------------------------------------------------------------
# Constraints
# --------------------------------------------------------------------------
StaffCodeStr = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-.]+$"),
]


# --------------------------------------------------------------------------
# Staff profile
# --------------------------------------------------------------------------
class StaffProfileCreateRequest(BaseModel):
    """POST /staff — invite a new staff member at the caller's agency.

    The user account is created in `INVITED` status; an invitation email
    is sent (out of scope for this module). `full_name` and `email` are
    forwarded to the identity module.
    """

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    full_name: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    phone: Annotated[str, StringConstraints(max_length=32)] | None = None
    staff_code: StaffCodeStr
    hired_at: date | None = None


class StaffProfileUpdateRequest(BaseModel):
    """PATCH /staff/{id} — partial update.

    Only fields explicitly set are applied. Omitted fields are unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    full_name: Annotated[str, StringConstraints(min_length=1, max_length=255)] | None = None
    phone: Annotated[str, StringConstraints(max_length=32)] | None = None
    staff_code: StaffCodeStr | None = None
    hired_at: date | None = None
    terminated_at: date | None = None
    status: UserStatus | None = None


class StaffProfileResponse(BaseModel):
    """Single staff profile, optionally with nested qualifications + availability."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    agency_id: UUID
    user_id: UUID
    staff_code: str
    status: UserStatus
    hired_at: date | None
    terminated_at: date | None
    created_at: datetime
    updated_at: datetime
    # Optional nested projections — populated only by endpoints that
    # opt in (e.g. GET /staff/{id}/with-details).
    qualifications: list["StaffQualificationResponse"] | None = None
    availability: list["StaffAvailabilityResponse"] | None = None


class StaffProfileSummaryResponse(BaseModel):
    """Lighter shape for list endpoints — no nested children."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    agency_id: UUID
    user_id: UUID
    staff_code: str
    status: UserStatus
    hired_at: date | None
    terminated_at: date | None
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------
# Qualifications
# --------------------------------------------------------------------------
class StaffQualificationCreateRequest(BaseModel):
    """POST /staff/{id}/qualifications — add a credential to a staff member."""

    model_config = ConfigDict(extra="forbid")

    qualification_type: QualificationType
    program_type: ProgramType | None = Field(
        default=None,
        description=(
            "Optional. If set, the qualification is scoped to that program. "
            "If null, the qualification applies to all programs the agency offers."
        ),
    )
    document_storage_key: Annotated[str, StringConstraints(max_length=512)] | None = None
    issued_at: date | None = None
    expires_at: date | None = None
    status: QualificationStatus = QualificationStatus.PENDING_VERIFICATION

    @model_validator(mode="after")
    def _validate_dates(self) -> "StaffQualificationCreateRequest":
        if self.issued_at and self.expires_at and self.expires_at < self.issued_at:
            raise ValueError("expires_at must be on or after issued_at")
        return self


class StaffQualificationUpdateRequest(BaseModel):
    """PATCH /staff/{id}/qualifications/{qual_id}.

    Used to attach a verified document (set `document_storage_key`) or to
    flip status (PENDING_VERIFICATION → ACTIVE / REVOKED / EXPIRED).
    """

    model_config = ConfigDict(extra="forbid")

    document_storage_key: Annotated[str, StringConstraints(max_length=512)] | None = None
    issued_at: date | None = None
    expires_at: date | None = None
    status: QualificationStatus | None = None
    program_type: ProgramType | None = None

    @model_validator(mode="after")
    def _validate_dates(self) -> "StaffQualificationUpdateRequest":
        if self.issued_at and self.expires_at and self.expires_at < self.issued_at:
            raise ValueError("expires_at must be on or after issued_at")
        return self


class StaffQualificationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    staff_id: UUID
    agency_id: UUID
    qualification_type: QualificationType
    program_type: ProgramType | None
    document_storage_key: str | None
    issued_at: date | None
    expires_at: date | None
    status: QualificationStatus
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------
class _AvailabilityBase(BaseModel):
    """Shared fields for availability create/update.

    Two flavours:
    - **Recurring weekly**: `day_of_week` + `start_time` + `end_time`
    - **One-off**:        `specific_date` (+ optional `specific_start/end`)

    `is_unavailable = false` means "I'm free"; `true` means "block me out".
    """

    model_config = ConfigDict(extra="forbid")

    is_unavailable: bool = False
    reason: Annotated[str, StringConstraints(max_length=512)] | None = None

    # Recurring
    day_of_week: Annotated[int, Field(ge=0, le=6)] | None = Field(
        default=None,
        description="0=Monday .. 6=Sunday (ISO weekday - 1).",
    )
    start_time: time | None = None
    end_time: time | None = None

    # One-off
    specific_date: date | None = None
    specific_start: datetime | None = None
    specific_end: datetime | None = None

    @model_validator(mode="after")
    def _validate_flavor(self) -> "_AvailabilityBase":
        recurring = self.day_of_week is not None
        one_off = self.specific_date is not None
        if recurring == one_off:  # both set or both unset
            raise ValueError(
                "Provide exactly one of: a recurring weekly window "
                "(day_of_week + start_time + end_time) or a one-off block "
                "(specific_date + optional specific_start/end)."
            )
        if recurring:
            if self.start_time is None or self.end_time is None:
                raise ValueError("Recurring availability requires start_time and end_time")
            if self.end_time <= self.start_time:
                raise ValueError("end_time must be after start_time")
        if one_off and self.specific_start and self.specific_end:
            if self.specific_end <= self.specific_start:
                raise ValueError("specific_end must be after specific_start")
        return self


class StaffAvailabilityCreateRequest(_AvailabilityBase):
    """POST /staff/{id}/availability — add a new availability row."""


class StaffAvailabilityUpdateRequest(BaseModel):
    """PATCH /staff/{id}/availability/{avail_id}.

    Availability rows are normally immutable; this exists so the staff
    member can flip `is_unavailable` (e.g. "actually I AM free on Friday")
    or change the `reason`. To change a window, DELETE + re-create.
    """

    model_config = ConfigDict(extra="forbid")

    is_unavailable: bool | None = None
    reason: Annotated[str, StringConstraints(max_length=512)] | None = None


class StaffAvailabilityResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    staff_id: UUID
    agency_id: UUID
    is_unavailable: bool
    day_of_week: int | None
    start_time: time | None
    end_time: time | None
    specific_date: date | None
    specific_start: datetime | None
    specific_end: datetime | None
    reason: str | None
    created_at: datetime


# --------------------------------------------------------------------------
# Resolving forward refs
# --------------------------------------------------------------------------
# StaffProfileResponse references the qualification/availability
# responses, so Pydantic needs the model registry complete before the
# forward ref can be resolved. This block is a no-op at import time and
# is a safety net for `from __future__ import annotations`-style string
# resolution.
StaffProfileResponse.model_rebuild()

__all__ = [
    "StaffAvailabilityCreateRequest",
    "StaffAvailabilityResponse",
    "StaffAvailabilityUpdateRequest",
    "StaffProfileCreateRequest",
    "StaffProfileResponse",
    "StaffProfileSummaryResponse",
    "StaffProfileUpdateRequest",
    "StaffQualificationCreateRequest",
    "StaffQualificationResponse",
    "StaffQualificationUpdateRequest",
]
