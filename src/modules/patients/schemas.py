"""Patients module — request/response Pydantic schemas (DTOs).

Wire format for every patient + guardian + relationship endpoint.

Pattern:
- `*Request`  — what the client sends (validated, trimmed)
- `*Response` — what we return
- Nested `*Nested` — child resources inlined in a parent response

All identifiers are UUIDs. Dates are ISO-8601.

See `13_DATABASE_SCHEMA_COMPLETE.md` §7 for the data model.
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

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    full_name: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    phone: Annotated[str, StringConstraints(max_length=32)] | None = None
    patient_code: PatientCodeStr
    date_of_birth: date | None = None
    gender: Annotated[str, StringConstraints(max_length=64)] | None = None
    preferred_language: Annotated[str, StringConstraints(max_length=64)] | None = None
    admitted_at: date | None = None


class PatientProfileUpdateRequest(BaseModel):
    """PATCH /patients/{id} — partial update.

    Only fields explicitly set are applied. Omitted fields are unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    full_name: Annotated[str, StringConstraints(min_length=1, max_length=255)] | None = None
    phone: Annotated[str, StringConstraints(max_length=32)] | None = None
    patient_code: PatientCodeStr | None = None
    date_of_birth: date | None = None
    gender: Annotated[str, StringConstraints(max_length=64)] | None = None
    preferred_language: Annotated[str, StringConstraints(max_length=64)] | None = None
    care_notes: Annotated[str, StringConstraints(max_length=4000)] | None = None
    admitted_at: date | None = None
    discharged_at: date | None = None
    status: UserStatus | None = None


class PatientProfileResponse(BaseModel):
    """Single patient profile, optionally with nested guardian relationships."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    agency_id: UUID
    user_id: UUID
    patient_code: str
    status: UserStatus
    date_of_birth: date | None
    gender: str | None
    preferred_language: str | None
    care_notes: str | None
    admitted_at: date | None
    discharged_at: date | None
    created_at: datetime
    updated_at: datetime
    # Optional nested — populated by GET /patients/{id}/with-relationships
    guardian_links: list["PatientGuardianRelationshipResponse"] | None = None


class PatientProfileSummaryResponse(BaseModel):
    """Lighter shape for list endpoints — no nested children."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    agency_id: UUID
    user_id: UUID
    patient_code: str
    status: UserStatus
    date_of_birth: date | None
    admitted_at: date | None
    discharged_at: date | None
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------
# Guardian profile
# --------------------------------------------------------------------------
class GuardianProfileCreateRequest(BaseModel):
    """POST /guardians — add a guardian at the caller's agency."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    full_name: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    phone: Annotated[str, StringConstraints(max_length=32)] | None = None
    contact_phone: Annotated[str, StringConstraints(max_length=32)] | None = None
    contact_email: EmailStr | None = None
    notes: Annotated[str, StringConstraints(max_length=4000)] | None = None


class GuardianProfileUpdateRequest(BaseModel):
    """PATCH /guardians/{id} — partial update."""

    model_config = ConfigDict(extra="forbid")

    full_name: Annotated[str, StringConstraints(min_length=1, max_length=255)] | None = None
    phone: Annotated[str, StringConstraints(max_length=32)] | None = None
    contact_phone: Annotated[str, StringConstraints(max_length=32)] | None = None
    contact_email: EmailStr | None = None
    notes: Annotated[str, StringConstraints(max_length=4000)] | None = None
    status: UserStatus | None = None


class GuardianProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    agency_id: UUID
    user_id: UUID
    status: UserStatus
    contact_phone: str | None
    contact_email: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


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

    model_config = ConfigDict(extra="forbid")

    relationship_type: RelationshipType
    is_legal: bool = False
    valid_from: date | None = None
    valid_until: date | None = None

    # Either reference an existing guardian OR provide a full create body.
    guardian_id: UUID | None = None
    new_guardian: "GuardianProfileCreateRequest | None" = None

    @model_validator(mode="after")
    def _validate_one_source(self) -> "PatientGuardianRelationshipCreateRequest":
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

    model_config = ConfigDict(extra="forbid")

    is_legal: bool | None = None
    valid_from: date | None = None
    valid_until: date | None = None

    @model_validator(mode="after")
    def _validate_dates(self) -> "PatientGuardianRelationshipUpdateRequest":
        if self.valid_from and self.valid_until and self.valid_until < self.valid_from:
            raise ValueError("valid_until must be on or after valid_from")
        return self


class PatientGuardianRelationshipResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    agency_id: UUID
    patient_id: UUID
    guardian_id: UUID
    relationship_type: RelationshipType
    is_legal: bool
    valid_from: date | None
    valid_until: date | None
    created_at: datetime
    updated_at: datetime


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