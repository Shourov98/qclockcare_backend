"""Staff module — request/response Pydantic schemas (DTOs).

The wire format for every staff endpoint. Pattern:
- `*Request`  — what the client sends (validated, trimmed)
- `*Response` — what we return (excludes secrets / internal-only fields)
- Nested `*Nested` — child resources inlined in a parent response

All identifiers are UUIDs (validated as strings — clients may pass opaque
strings). Date / time fields are ISO-8601.

See `13_DATABASE_SCHEMA_COMPLETE.md` §6 for the data model.

Every model carries `Field(description=...)` on each field and
`model_config(json_schema_extra={"examples": [...]})` so `/docs`
shows realistic request/response examples and Swagger UI's "Try it
out" pre-fills with sensible values.
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

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "email": "jenna.lopez@careagency.com",
                    "full_name": "Jenna Lopez",
                    "phone": "+1-612-555-0142",
                    "staff_code": "STAFF-0042",
                    "hired_at": "2026-06-01",
                }
            ]
        },
    )

    email: EmailStr = Field(
        description=(
            "Work email of the new staff member. Must be globally unique "
            "across the system — `DUPLICATE_RESOURCE` if already taken."
        ),
    )
    full_name: Annotated[
        str, StringConstraints(min_length=1, max_length=255)
    ] = Field(
        description="Display name (first + last). Used in the invitation email.",
    )
    phone: Annotated[str, StringConstraints(max_length=32)] | None = Field(
        default=None,
        description="Optional. E.164-format preferred (e.g. `+1-612-555-0142`).",
    )
    staff_code: StaffCodeStr = Field(
        description=(
            "Agency-scoped identifier (alphanumeric, dashes, underscores, "
            "dots; max 64 chars). Visible to clients in roster lists."
        ),
    )
    hired_at: date | None = Field(
        default=None,
        description="Start date. Defaults to today if omitted.",
    )


class StaffProfileUpdateRequest(BaseModel):
    """PATCH /staff/{id} — partial update.

    Only fields explicitly set are applied. Omitted fields are unchanged.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "full_name": "Jenna M. Lopez",
                    "phone": "+1-612-555-9999",
                    "status": "INACTIVE",
                }
            ]
        },
    )

    full_name: Annotated[
        str, StringConstraints(min_length=1, max_length=255)
    ] | None = Field(
        default=None,
        description="New display name (omit to leave unchanged).",
    )
    phone: Annotated[str, StringConstraints(max_length=32)] | None = Field(
        default=None,
        description="New phone (omit to leave unchanged). Send `null` to clear.",
    )
    staff_code: StaffCodeStr | None = Field(
        default=None,
        description="New staff code (omit to leave unchanged).",
    )
    hired_at: date | None = Field(
        default=None,
        description="New hire date (omit to leave unchanged).",
    )
    terminated_at: date | None = Field(
        default=None,
        description="Termination date. Setting this typically also flips `status` to `INACTIVE`.",
    )
    status: UserStatus | None = Field(
        default=None,
        description="New lifecycle status (omit to leave unchanged).",
    )


class StaffProfileResponse(BaseModel):
    """Single staff profile, optionally with nested qualifications + availability."""

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "id": "7c2e9b51-4a8d-4f5e-9c1a-2b3d4e5f6a7b",
                    "agency_id": "8a3f12d0-7b5e-4a23-9c8e-1b2c3d4e5f6a",
                    "user_id": "5f3a7b1c-1d0a-4a23-9c8e-1b2c3d4e5f6a",
                    "staff_code": "STAFF-0042",
                    "status": "ACTIVE",
                    "hired_at": "2026-06-01",
                    "terminated_at": None,
                    "created_at": "2026-06-01T14:00:00Z",
                    "updated_at": "2026-06-15T09:30:00Z",
                }
            ]
        },
    )

    id: UUID = Field(description="Staff profile UUID.")
    agency_id: UUID = Field(description="Owning agency UUID.")
    user_id: UUID = Field(
        description="Underlying user account UUID (link to `/auth/me`).",
    )
    staff_code: str = Field(description="Agency-scoped identifier.")
    status: UserStatus = Field(
        description="Lifecycle status (`INVITED`, `ACTIVE`, `INACTIVE`, ...).",
    )
    hired_at: date | None = Field(description="Start date.")
    terminated_at: date | None = Field(description="Termination date, or null if active.")
    created_at: datetime = Field(description="UTC ISO-8601 of row creation.")
    updated_at: datetime = Field(description="UTC ISO-8601 of last mutation.")
    # Optional nested projections — populated only by endpoints that
    # opt in (e.g. GET /staff/{id}/with-details).
    qualifications: list[StaffQualificationResponse] | None = Field(
        default=None,
        description="Populated by `GET /staff/{id}/with-details`. `null` otherwise.",
    )
    availability: list[StaffAvailabilityResponse] | None = Field(
        default=None,
        description="Populated by `GET /staff/{id}/with-details`. `null` otherwise.",
    )


class StaffProfileSummaryResponse(BaseModel):
    """Lighter shape for list endpoints — no nested children."""

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "id": "7c2e9b51-4a8d-4f5e-9c1a-2b3d4e5f6a7b",
                    "agency_id": "8a3f12d0-7b5e-4a23-9c8e-1b2c3d4e5f6a",
                    "user_id": "5f3a7b1c-1d0a-4a23-9c8e-1b2c3d4e5f6a",
                    "staff_code": "STAFF-0042",
                    "status": "ACTIVE",
                    "hired_at": "2026-06-01",
                    "terminated_at": None,
                    "created_at": "2026-06-01T14:00:00Z",
                    "updated_at": "2026-06-15T09:30:00Z",
                }
            ]
        },
    )

    id: UUID = Field(description="Staff profile UUID.")
    agency_id: UUID = Field(description="Owning agency UUID.")
    user_id: UUID = Field(description="Underlying user account UUID.")
    staff_code: str = Field(description="Agency-scoped identifier.")
    status: UserStatus = Field(description="Lifecycle status.")
    hired_at: date | None = Field(description="Start date.")
    terminated_at: date | None = Field(description="Termination date, or null.")
    created_at: datetime = Field(description="UTC ISO-8601 of row creation.")
    updated_at: datetime = Field(description="UTC ISO-8601 of last mutation.")


# --------------------------------------------------------------------------
# Qualifications
# --------------------------------------------------------------------------
class StaffQualificationCreateRequest(BaseModel):
    """POST /staff/{id}/qualifications — add a credential to a staff member."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "qualification_type": "PCA_CERTIFIED",
                    "program_type": "PCA",
                    "document_storage_key": "agencies/8a3f12d0/staff/7c2e9b51/pca-cert.pdf",
                    "issued_at": "2026-01-15",
                    "expires_at": "2028-01-15",
                    "status": "PENDING_VERIFICATION",
                }
            ]
        },
    )

    qualification_type: QualificationType = Field(
        description=(
            "Type of credential (`PCA_CERTIFIED`, `CPR`, `RN`, `LPN`, "
            "`CNA`, `ARMHS_PROVIDER`, etc.)."
        ),
    )
    program_type: ProgramType | None = Field(
        default=None,
        description=(
            "Optional. If set, the qualification is scoped to that program. "
            "If null, the qualification applies to all programs the agency offers."
        ),
    )
    document_storage_key: Annotated[str, StringConstraints(max_length=512)] | None = Field(
        default=None,
        description=(
            "Object key of the uploaded credential document in S3 / Supabase "
            "Storage. Set after the upload completes; null until then."
        ),
    )
    issued_at: date | None = Field(default=None, description="Date the credential was issued.")
    expires_at: date | None = Field(default=None, description="Expiration date (if any).")
    status: QualificationStatus = Field(
        default=QualificationStatus.PENDING_VERIFICATION,
        description=(
            "Lifecycle status. New rows default to `PENDING_VERIFICATION` "
            "until an admin reviews the uploaded document."
        ),
    )

    @model_validator(mode="after")
    def _validate_dates(self) -> StaffQualificationCreateRequest:
        if self.issued_at and self.expires_at and self.expires_at < self.issued_at:
            raise ValueError("expires_at must be on or after issued_at")
        return self


class StaffQualificationUpdateRequest(BaseModel):
    """PATCH /staff/{id}/qualifications/{qual_id}.

    Used to attach a verified document (set `document_storage_key`) or to
    flip status (PENDING_VERIFICATION → ACTIVE / REVOKED / EXPIRED).
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "document_storage_key": "agencies/8a3f12d0/staff/7c2e9b51/pca-cert.pdf",
                    "status": "ACTIVE",
                }
            ]
        },
    )

    document_storage_key: Annotated[str, StringConstraints(max_length=512)] | None = Field(
        default=None,
        description="Object key for the uploaded document. Set after upload.",
    )
    issued_at: date | None = Field(default=None, description="Updated issue date.")
    expires_at: date | None = Field(default=None, description="Updated expiration date.")
    status: QualificationStatus | None = Field(
        default=None,
        description="Updated status (typically `PENDING_VERIFICATION` → `ACTIVE`).",
    )
    program_type: ProgramType | None = Field(
        default=None,
        description="Updated program scope.",
    )

    @model_validator(mode="after")
    def _validate_dates(self) -> StaffQualificationUpdateRequest:
        if self.issued_at and self.expires_at and self.expires_at < self.issued_at:
            raise ValueError("expires_at must be on or after issued_at")
        return self


class StaffQualificationResponse(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "id": "9a8b7c6d-5e4f-3a2b-1c0d-9e8f7a6b5c4d",
                    "staff_id": "7c2e9b51-4a8d-4f5e-9c1a-2b3d4e5f6a7b",
                    "agency_id": "8a3f12d0-7b5e-4a23-9c8e-1b2c3d4e5f6a",
                    "qualification_type": "PCA_CERTIFIED",
                    "program_type": "PCA",
                    "download_url": None,
                    "expires_in": None,
                    "issued_at": "2026-01-15",
                    "expires_at": "2028-01-15",
                    "status": "ACTIVE",
                    "created_at": "2026-06-01T14:05:00Z",
                    "updated_at": "2026-06-02T10:00:00Z",
                }
            ]
        },
    )

    id: UUID = Field(description="Qualification row UUID.")
    staff_id: UUID = Field(description="Owning staff profile UUID.")
    agency_id: UUID = Field(description="Agency UUID (denormalised).")
    qualification_type: QualificationType = Field(description="Credential type.")
    program_type: ProgramType | None = Field(description="Program scope, or null.")
    # Short-lived signed download URL, or None if the qualification has
    # no attached document. Generated server-side from the underlying
    # storage key — the raw key is never returned to clients.
    download_url: str | None = Field(
        default=None,
        description=(
            "Short-lived signed URL for the uploaded document. "
            "`null` until `document_storage_key` is set. URL lifetime is "
            "`settings.S3_PRESIGNED_URL_TTL_SECONDS`."
        ),
    )
    # Storage key TTL in seconds (matches `settings.S3_PRESIGNED_URL_TTL_SECONDS`).
    # `None` when `download_url` is None. Surfaced so the client can
    # show "expires in N minutes" or schedule a refresh.
    expires_in: int | None = Field(
        default=None,
        description=(
            "Seconds until `download_url` expires. Populated alongside "
            "`download_url`; `null` when there's no attached document."
        ),
    )
    issued_at: date | None = Field(description="Issue date, or null.")
    expires_at: date | None = Field(description="Expiration date, or null.")
    status: QualificationStatus = Field(description="Lifecycle status.")
    created_at: datetime = Field(description="UTC ISO-8601 of row creation.")
    updated_at: datetime = Field(description="UTC ISO-8601 of last mutation.")


class QualificationDownloadResponse(BaseModel):
    """GET /staff/{staff_id}/qualifications/{qualification_id}/download.

    Returns a short-lived signed URL the client can hand to a browser.
    `expires_in` and `expires_at` are the same number expressed two
    ways — `expires_in` for display ("expires in N minutes"),
    `expires_at` for scheduling a refresh.
    """

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "download_url": "https://qlockcare-prod.s3.amazonaws.com/...?X-Amz-Signature=...",
                    "expires_in": 900,
                    "expires_at": "2026-06-28T10:38:00Z",
                }
            ]
        },
    )

    download_url: str = Field(
        description=(
            "Presigned URL for the credential document. Hand to a browser "
            "or `wget` — no further auth needed."
        ),
    )
    expires_in: int = Field(
        ge=60,
        le=86400,
        description="Seconds until the URL stops working.",
    )
    expires_at: datetime = Field(
        description="UTC ISO-8601 absolute expiration time.",
    )


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

    is_unavailable: bool = Field(
        default=False,
        description=(
            "`false` = availability window (I'm free). "
            "`true` = unavailability block (block me out). "
            "Same shape either way — the `reason` distinguishes them."
        ),
    )
    reason: Annotated[str, StringConstraints(max_length=512)] | None = Field(
        default=None,
        description="Free-text note (e.g. \"Doctor appointment\", \"Weekend shift\").",
    )

    # Recurring
    day_of_week: Annotated[int, Field(ge=0, le=6)] | None = Field(
        default=None,
        description="0=Monday .. 6=Sunday (ISO weekday - 1). Set for recurring weekly windows.",
    )
    start_time: time | None = Field(
        default=None,
        description="Start time of the recurring window (24h, e.g. `09:00`). Required when `day_of_week` is set.",
    )
    end_time: time | None = Field(
        default=None,
        description="End time of the recurring window (must be after `start_time`). Required when `day_of_week` is set.",
    )

    # One-off
    specific_date: date | None = Field(
        default=None,
        description="Calendar date for a one-off block. Set instead of `day_of_week`.",
    )
    specific_start: datetime | None = Field(
        default=None,
        description="Start of a one-off block (UTC ISO-8601). Optional when `specific_date` is set.",
    )
    specific_end: datetime | None = Field(
        default=None,
        description="End of a one-off block (UTC ISO-8601, must be after `specific_start`).",
    )

    @model_validator(mode="after")
    def _validate_flavor(self) -> _AvailabilityBase:
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

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "is_unavailable": False,
                    "day_of_week": 0,
                    "start_time": "09:00:00",
                    "end_time": "17:00:00",
                    "reason": "Monday day shift",
                },
                {
                    "is_unavailable": True,
                    "specific_date": "2026-07-04",
                    "specific_start": "2026-07-04T00:00:00Z",
                    "specific_end": "2026-07-05T00:00:00Z",
                    "reason": "Independence Day",
                },
            ]
        },
    )


class StaffAvailabilityUpdateRequest(BaseModel):
    """PATCH /staff/{id}/availability/{avail_id}.

    Availability rows are normally immutable; this exists so the staff
    member can flip `is_unavailable` (e.g. "actually I AM free on Friday")
    or change the `reason`. To change a window, DELETE + re-create.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"is_unavailable": True, "reason": "Sick — call me"}
            ]
        },
    )

    is_unavailable: bool | None = Field(
        default=None,
        description="Flip the available/blocked flag (omit to leave unchanged).",
    )
    reason: Annotated[str, StringConstraints(max_length=512)] | None = Field(
        default=None,
        description="Updated reason text.",
    )


class StaffAvailabilityResponse(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "id": "1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d",
                    "staff_id": "7c2e9b51-4a8d-4f5e-9c1a-2b3d4e5f6a7b",
                    "agency_id": "8a3f12d0-7b5e-4a23-9c8e-1b2c3d4e5f6a",
                    "is_unavailable": False,
                    "day_of_week": 0,
                    "start_time": "09:00:00",
                    "end_time": "17:00:00",
                    "specific_date": None,
                    "specific_start": None,
                    "specific_end": None,
                    "reason": "Monday day shift",
                    "created_at": "2026-06-01T14:10:00Z",
                }
            ]
        },
    )

    id: UUID = Field(description="Availability row UUID.")
    staff_id: UUID = Field(description="Owning staff profile UUID.")
    agency_id: UUID = Field(description="Agency UUID (denormalised).")
    is_unavailable: bool = Field(description="`false` = available window, `true` = blocked.")
    day_of_week: int | None = Field(description="0=Mon..6=Sun for recurring, null for one-off.")
    start_time: time | None = Field(description="Recurring window start, null for one-off.")
    end_time: time | None = Field(description="Recurring window end, null for one-off.")
    specific_date: date | None = Field(description="One-off date, null for recurring.")
    specific_start: datetime | None = Field(description="One-off start (UTC).")
    specific_end: datetime | None = Field(description="One-off end (UTC).")
    reason: str | None = Field(description="Free-text note.")
    created_at: datetime = Field(description="UTC ISO-8601 of row creation.")


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
