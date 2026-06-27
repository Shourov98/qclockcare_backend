"""Patients module — request/response Pydantic schemas (DTOs).

Wire format for every patient + guardian + relationship endpoint.

Pattern:
- `*Request`  — what the client sends (validated, trimmed)
- `*Response` — what we return
- Nested `*Nested` — child resources inlined in a parent response

All identifiers are UUIDs. Dates are ISO-8601.

See `13_DATABASE_SCHEMA_COMPLETE.md` §7 for the data model.

Every model carries `Field(description=...)` on each field and
`model_config(json_schema_extra={"examples": [...]})` so `/docs`
shows realistic request/response examples and Swagger UI's "Try it
out" pre-fills with sensible values.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    model_validator,
)

from src.shared.domain.enums import RelationshipType, UserStatus

# --------------------------------------------------------------------------
# Constraints
# --------------------------------------------------------------------------
PatientCodeStr = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-.]+$"),
]


# --------------------------------------------------------------------------
# Patient profile
# --------------------------------------------------------------------------
class PatientProfileCreateRequest(BaseModel):
    """POST /patients — admit a new patient at the caller's agency."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "email": "maria.santos@example.com",
                    "full_name": "Maria Santos",
                    "phone": "+1-612-555-0188",
                    "patient_code": "PT-1024",
                    "date_of_birth": "1948-03-14",
                    "gender": "female",
                    "preferred_language": "es",
                    "admitted_at": "2026-06-15",
                }
            ]
        },
    )

    email: EmailStr = Field(
        description=(
            "Contact email for the patient (or their primary guardian "
            "if the patient can't manage email themselves). Must be "
            "globally unique — `DUPLICATE_RESOURCE` if already taken."
        ),
    )
    full_name: Annotated[
        str, StringConstraints(min_length=1, max_length=255)
    ] = Field(description="Display name (first + last).")
    phone: Annotated[str, StringConstraints(max_length=32)] | None = Field(
        default=None,
        description="E.164-format phone (e.g. `+1-612-555-0188`).",
    )
    patient_code: PatientCodeStr = Field(
        description=(
            "Agency-scoped identifier (alphanumeric, dashes, "
            "underscores, dots; max 64 chars). Visible in roster lists."
        ),
    )
    date_of_birth: date | None = Field(
        default=None,
        description="Calendar date of birth. Used for age-based care rules.",
    )
    gender: Annotated[str, StringConstraints(max_length=64)] | None = Field(
        default=None,
        description="Free-text gender label (`female`, `male`, `non-binary`, etc.).",
    )
    preferred_language: Annotated[str, StringConstraints(max_length=64)] | None = Field(
        default=None,
        description=(
            "ISO 639-1 code preferred by the patient (`en`, `es`, "
            "`so`, `hmn`, ...) — used to pick interpreters."
        ),
    )
    admitted_at: date | None = Field(
        default=None,
        description="Admission date. Defaults to today if omitted.",
    )


class PatientProfileUpdateRequest(BaseModel):
    """PATCH /patients/{id} — partial update.

    Only fields explicitly set are applied. Omitted fields are unchanged.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "preferred_language": "en",
                    "care_notes": "Allergic to penicillin. Hearing aid in left ear.",
                }
            ]
        },
    )

    full_name: Annotated[
        str, StringConstraints(min_length=1, max_length=255)
    ] | None = Field(default=None, description="New display name.")
    phone: Annotated[str, StringConstraints(max_length=32)] | None = Field(
        default=None,
        description="New phone. Send `null` to clear.",
    )
    patient_code: PatientCodeStr | None = Field(
        default=None, description="New patient code."
    )
    date_of_birth: date | None = Field(default=None, description="New DOB.")
    gender: Annotated[str, StringConstraints(max_length=64)] | None = Field(
        default=None, description="New gender label."
    )
    preferred_language: Annotated[str, StringConstraints(max_length=64)] | None = Field(
        default=None, description="New preferred language code."
    )
    care_notes: Annotated[str, StringConstraints(max_length=4000)] | None = Field(
        default=None,
        description=(
            "Free-text care notes (allergies, mobility aids, "
            "communication preferences). Visible to assigned staff."
        ),
    )
    admitted_at: date | None = Field(default=None, description="New admission date.")
    discharged_at: date | None = Field(
        default=None,
        description="Discharge date. Setting this typically also flips `status` to `INACTIVE`.",
    )
    status: UserStatus | None = Field(
        default=None, description="New lifecycle status."
    )


class PatientProfileResponse(BaseModel):
    """Single patient profile, optionally with nested guardian relationships."""

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "id": "4d3c2b1a-0e9f-8d7c-6b5a-4f3e2d1c0b9a",
                    "agency_id": "8a3f12d0-7b5e-4a23-9c8e-1b2c3d4e5f6a",
                    "user_id": "5f3a7b1c-1d0a-4a23-9c8e-1b2c3d4e5f6a",
                    "patient_code": "PT-1024",
                    "status": "ACTIVE",
                    "date_of_birth": "1948-03-14",
                    "gender": "female",
                    "preferred_language": "es",
                    "care_notes": "Allergic to penicillin.",
                    "admitted_at": "2026-06-15",
                    "discharged_at": None,
                    "created_at": "2026-06-15T10:00:00Z",
                    "updated_at": "2026-06-20T14:30:00Z",
                }
            ]
        },
    )

    id: UUID = Field(description="Patient profile UUID.")
    agency_id: UUID = Field(description="Owning agency UUID.")
    user_id: UUID = Field(
        description="Underlying user account UUID (link to `/auth/me`).",
    )
    patient_code: str = Field(description="Agency-scoped identifier.")
    status: UserStatus = Field(description="Lifecycle status.")
    date_of_birth: date | None = Field(description="Calendar date of birth, or null.")
    gender: str | None = Field(description="Gender label, or null.")
    preferred_language: str | None = Field(description="Preferred language code, or null.")
    care_notes: str | None = Field(description="Free-text care notes, or null.")
    admitted_at: date | None = Field(description="Admission date, or null.")
    discharged_at: date | None = Field(description="Discharge date, or null.")
    created_at: datetime = Field(description="UTC ISO-8601 of row creation.")
    updated_at: datetime = Field(description="UTC ISO-8601 of last mutation.")
    # Optional nested — populated by GET /patients/{id}/with-relationships
    guardian_links: list[PatientGuardianRelationshipResponse] | None = Field(
        default=None,
        description=(
            "Populated by `GET /patients/{id}/with-relationships`. "
            "`null` otherwise."
        ),
    )


class PatientProfileSummaryResponse(BaseModel):
    """Lighter shape for list endpoints — no nested children."""

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "id": "4d3c2b1a-0e9f-8d7c-6b5a-4f3e2d1c0b9a",
                    "agency_id": "8a3f12d0-7b5e-4a23-9c8e-1b2c3d4e5f6a",
                    "user_id": "5f3a7b1c-1d0a-4a23-9c8e-1b2c3d4e5f6a",
                    "patient_code": "PT-1024",
                    "status": "ACTIVE",
                    "date_of_birth": "1948-03-14",
                    "admitted_at": "2026-06-15",
                    "discharged_at": None,
                    "created_at": "2026-06-15T10:00:00Z",
                    "updated_at": "2026-06-20T14:30:00Z",
                }
            ]
        },
    )

    id: UUID = Field(description="Patient profile UUID.")
    agency_id: UUID = Field(description="Owning agency UUID.")
    user_id: UUID = Field(description="Underlying user account UUID.")
    patient_code: str = Field(description="Agency-scoped identifier.")
    status: UserStatus = Field(description="Lifecycle status.")
    date_of_birth: date | None = Field(description="Calendar date of birth, or null.")
    admitted_at: date | None = Field(description="Admission date, or null.")
    discharged_at: date | None = Field(description="Discharge date, or null.")
    created_at: datetime = Field(description="UTC ISO-8601 of row creation.")
    updated_at: datetime = Field(description="UTC ISO-8601 of last mutation.")


# --------------------------------------------------------------------------
# Guardian profile
# --------------------------------------------------------------------------
class GuardianProfileCreateRequest(BaseModel):
    """POST /guardians — add a guardian at the caller's agency."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "email": "rosa.santos@example.com",
                    "full_name": "Rosa Santos",
                    "phone": "+1-612-555-0177",
                    "contact_phone": "+1-612-555-0177",
                    "contact_email": "rosa.santos@example.com",
                    "notes": "Daughter; primary emergency contact.",
                }
            ]
        },
    )

    email: EmailStr = Field(
        description=(
            "Work or personal email of the guardian. Must be globally "
            "unique — `DUPLICATE_RESOURCE` if already taken."
        ),
    )
    full_name: Annotated[
        str, StringConstraints(min_length=1, max_length=255)
    ] = Field(description="Display name (first + last).")
    phone: Annotated[str, StringConstraints(max_length=32)] | None = Field(
        default=None,
        description="Personal phone (may differ from `contact_phone`).",
    )
    contact_phone: Annotated[str, StringConstraints(max_length=32)] | None = Field(
        default=None,
        description=(
            "Phone to use for patient-care outreach. Defaults to "
            "`phone` if omitted."
        ),
    )
    contact_email: EmailStr | None = Field(
        default=None,
        description="Email for care-coordination outreach. Defaults to `email` if omitted.",
    )
    notes: Annotated[str, StringConstraints(max_length=4000)] | None = Field(
        default=None,
        description="Free-text notes (relationship, availability, language preferences).",
    )


class GuardianProfileUpdateRequest(BaseModel):
    """PATCH /guardians/{id} — partial update."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"notes": "Moved to new address — see linked contact card."}
            ]
        },
    )

    full_name: Annotated[
        str, StringConstraints(min_length=1, max_length=255)
    ] | None = Field(default=None, description="New display name.")
    phone: Annotated[str, StringConstraints(max_length=32)] | None = Field(
        default=None, description="New personal phone."
    )
    contact_phone: Annotated[str, StringConstraints(max_length=32)] | None = Field(
        default=None, description="New outreach phone."
    )
    contact_email: EmailStr | None = Field(
        default=None, description="New outreach email."
    )
    notes: Annotated[str, StringConstraints(max_length=4000)] | None = Field(
        default=None, description="Updated notes."
    )
    status: UserStatus | None = Field(default=None, description="New lifecycle status.")


class GuardianProfileResponse(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "id": "9e8d7c6b-5a4f-3e2d-1c0b-9a8f7e6d5c4b",
                    "agency_id": "8a3f12d0-7b5e-4a23-9c8e-1b2c3d4e5f6a",
                    "user_id": "5f3a7b1c-1d0a-4a23-9c8e-1b2c3d4e5f6a",
                    "status": "ACTIVE",
                    "contact_phone": "+1-612-555-0177",
                    "contact_email": "rosa.santos@example.com",
                    "notes": "Daughter; primary emergency contact.",
                    "created_at": "2026-06-15T10:05:00Z",
                    "updated_at": "2026-06-15T10:05:00Z",
                }
            ]
        },
    )

    id: UUID = Field(description="Guardian profile UUID.")
    agency_id: UUID = Field(description="Owning agency UUID.")
    user_id: UUID = Field(description="Underlying user account UUID.")
    status: UserStatus = Field(description="Lifecycle status.")
    contact_phone: str | None = Field(description="Outreach phone, or null.")
    contact_email: str | None = Field(description="Outreach email, or null.")
    notes: str | None = Field(description="Free-text notes, or null.")
    created_at: datetime = Field(description="UTC ISO-8601 of row creation.")
    updated_at: datetime = Field(description="UTC ISO-8601 of last mutation.")


# --------------------------------------------------------------------------
# Patient ↔ Guardian relationship
# --------------------------------------------------------------------------
class PatientGuardianRelationshipCreateRequest(BaseModel):
    """POST /patients/{id}/guardians — link a guardian to a patient.

    The guardian profile may be an existing `guardian_id` (preferred when
    re-using a known guardian across multiple patients) OR a full
    `GuardianProfileCreateRequest` body (one-shot create + link).
    Exactly one of the two must be supplied.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "relationship_type": "DAUGHTER",
                    "is_legal": True,
                    "guardian_id": "9e8d7c6b-5a4f-3e2d-1c0b-9a8f7e6d5c4b",
                },
                {
                    "relationship_type": "SON",
                    "is_legal": False,
                    "valid_from": "2026-06-15",
                    "new_guardian": {
                        "email": "luis.santos@example.com",
                        "full_name": "Luis Santos",
                        "contact_phone": "+1-612-555-0144",
                    },
                },
            ]
        },
    )

    relationship_type: RelationshipType = Field(
        description=(
            "Family or legal relationship (`DAUGHTER`, `SON`, "
            "`SPOUSE`, `GUARDIAN`, `POWER_OF_ATTORNEY`, etc.)."
        ),
    )
    is_legal: bool = Field(
        default=False,
        description=(
            "`true` if this guardian has legal authority to make "
            "care decisions (healthcare power of attorney, court-"
            "appointed guardian, etc.)."
        ),
    )
    valid_from: date | None = Field(
        default=None,
        description="Date the relationship becomes active.",
    )
    valid_until: date | None = Field(
        default=None,
        description=(
            "Optional expiry (e.g. temporary guardianship). "
            "After this date the link is treated as inactive."
        ),
    )

    # Either reference an existing guardian OR provide a full create body.
    guardian_id: UUID | None = Field(
        default=None,
        description=(
            "Existing guardian profile UUID to link. Use when the "
            "guardian already has an account at this agency."
        ),
    )
    new_guardian: GuardianProfileCreateRequest | None = Field(
        default=None,
        description=(
            "Inline `GuardianProfileCreateRequest` body to create a "
            "new guardian and link them in one shot. Use when the "
            "guardian is brand new."
        ),
    )

    @model_validator(mode="after")
    def _validate_one_source(self) -> PatientGuardianRelationshipCreateRequest:
        if (self.guardian_id is None) == (self.new_guardian is None):
            raise ValueError(
                "Provide exactly one of: guardian_id (existing guardian) "
                "or new_guardian (full create body)."
            )
        if self.valid_from and self.valid_until and self.valid_until < self.valid_from:
            raise ValueError("valid_until must be on or after valid_from")
        return self


class PatientGuardianRelationshipUpdateRequest(BaseModel):
    """PATCH /patient-guardian-relationships/{id} — partial update."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"is_legal": True},
                {"valid_until": "2026-12-31"},
            ]
        },
    )

    is_legal: bool | None = Field(
        default=None,
        description="Flip the legal-authority flag.",
    )
    valid_from: date | None = Field(default=None, description="Updated `valid_from`.")
    valid_until: date | None = Field(default=None, description="Updated `valid_until`.")

    @model_validator(mode="after")
    def _validate_dates(self) -> PatientGuardianRelationshipUpdateRequest:
        if self.valid_from and self.valid_until and self.valid_until < self.valid_from:
            raise ValueError("valid_until must be on or after valid_from")
        return self


class PatientGuardianRelationshipResponse(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "id": "1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d",
                    "agency_id": "8a3f12d0-7b5e-4a23-9c8e-1b2c3d4e5f6a",
                    "patient_id": "4d3c2b1a-0e9f-8d7c-6b5a-4f3e2d1c0b9a",
                    "guardian_id": "9e8d7c6b-5a4f-3e2d-1c0b-9a8f7e6d5c4b",
                    "relationship_type": "DAUGHTER",
                    "is_legal": True,
                    "valid_from": "2026-06-15",
                    "valid_until": None,
                    "created_at": "2026-06-15T10:10:00Z",
                    "updated_at": "2026-06-15T10:10:00Z",
                }
            ]
        },
    )

    id: UUID = Field(description="Relationship row UUID.")
    agency_id: UUID = Field(description="Owning agency UUID.")
    patient_id: UUID = Field(description="Patient profile UUID.")
    guardian_id: UUID = Field(description="Guardian profile UUID.")
    relationship_type: RelationshipType = Field(description="Family / legal relationship.")
    is_legal: bool = Field(description="`true` if the guardian has legal authority.")
    valid_from: date | None = Field(description="Effective date, or null.")
    valid_until: date | None = Field(description="Expiry date, or null.")
    created_at: datetime = Field(description="UTC ISO-8601 of row creation.")
    updated_at: datetime = Field(description="UTC ISO-8601 of last mutation.")


# --------------------------------------------------------------------------
# Forward refs
# --------------------------------------------------------------------------
PatientProfileResponse.model_rebuild()
PatientGuardianRelationshipCreateRequest.model_rebuild()


__all__ = [
    "GuardianProfileCreateRequest",
    "GuardianProfileResponse",
    "GuardianProfileUpdateRequest",
    "PatientGuardianRelationshipCreateRequest",
    "PatientGuardianRelationshipResponse",
    "PatientGuardianRelationshipUpdateRequest",
    "PatientProfileCreateRequest",
    "PatientProfileResponse",
    "PatientProfileSummaryResponse",
    "PatientProfileUpdateRequest",
]
