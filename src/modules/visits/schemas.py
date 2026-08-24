"""Visits module — request/response Pydantic schemas (DTOs).

Wire format for every visit + EVV + signature + activity + note endpoint.

Pattern:
- `*Request`  — what the client sends
- `*Response` — what we return
- Nested `*Nested` — child resources inlined in a parent response

The schemas are aligned with the canonical spec
(`QlockCare_appointemnt_flow.md`): the 5-state lifecycle, free-text
activities, EVV start+end records, and a required patient-or-guardian
signature. Verified values are dropped from the wire — the patient
either signs (`AppointmentSignature`) or doesn't.

See `13_DATABASE_SCHEMA_COMPLETE.md` §11-§12 for the data model and
status lifecycle.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

from src.shared.domain.enums import ServiceItemStatus, UserRole, VisitStatus


# --------------------------------------------------------------------------
# Visit — request
# --------------------------------------------------------------------------
class VisitCreateRequest(BaseModel):
    """POST /visits — start the visit (transitions `READY → IN_PROGRESS`).

    On success the server creates a Visit + EVVRecord (start filled
    in with the supplied GPS) + VisitActivityDelivery row per parent
    appointment activity.
    """

    model_config = ConfigDict(extra="forbid")

    appointment_id: UUID
    start_lat: Decimal | None = None
    start_lng: Decimal | None = None
    start_accuracy_m: Decimal | None = None
    start_device_id: Annotated[str, StringConstraints(max_length=512)] | None = None

    @model_validator(mode="after")
    def _validate_lat_lng_pair(self) -> VisitCreateRequest:
        if (self.start_lat is None) != (self.start_lng is None):
            raise ValueError(
                "start_lat and start_lng must both be set or both be null"
            )
        return self


class VisitEndRequest(BaseModel):
    """PATCH /visits/{id}/end — record the EVV end (caregiver departure)."""

    model_config = ConfigDict(extra="forbid")

    end_lat: Decimal | None = None
    end_lng: Decimal | None = None
    end_accuracy_m: Decimal | None = None


class VisitConfirmBillingRequest(BaseModel):
    """POST /visits/{id}/confirm-billing — caregiver ticks the billing
    confirmation checkbox.

    Required before the visit can transition to `AWAITING_SIGNATURE`
    (spec §6 / §11). Idempotent — re-confirming is a no-op.
    """

    model_config = ConfigDict(extra="forbid")


class VisitStatusTransitionRequest(BaseModel):
    """PATCH /visits/{id}/transition — walks the 5-state lifecycle.

    Allowed edges only (service-layer enforced):
      SCHEDULED → READY
      READY     → IN_PROGRESS
      IN_PROGRESS → AWAITING_SIGNATURE (gated: all activities + billing)
      AWAITING_SIGNATURE → COMPLETED (gated: signature exists)
      *         → CANCELLED / MISSED / REJECTED (per spec)
    """

    model_config = ConfigDict(extra="forbid")

    status: VisitStatus


class VisitLocationPingRequest(BaseModel):
    """POST /visits/{id}/location-ping — staff device updates its live GPS."""

    model_config = ConfigDict(extra="forbid")

    lat: Decimal
    lng: Decimal
    accuracy_m: Decimal | None = None
    device_id: Annotated[str, StringConstraints(max_length=512)] | None = None

    @model_validator(mode="after")
    def _validate_lat_lng_range(self) -> VisitLocationPingRequest:
        if not (-90 <= self.lat <= 90):
            raise ValueError("lat must be in [-90, 90]")
        if not (-180 <= self.lng <= 180):
            raise ValueError("lng must be in [-180, 180]")
        return self


class VisitStartLocationSharingRequest(BaseModel):
    """POST /visits/{id}/start-location-sharing — staff opts in to live GPS."""

    model_config = ConfigDict(extra="forbid")

    initial_lat: Decimal | None = None
    initial_lng: Decimal | None = None
    initial_accuracy_m: Decimal | None = None

    @model_validator(mode="after")
    def _validate_initial_lat_lng_pair(self) -> VisitStartLocationSharingRequest:
        if (self.initial_lat is None) != (self.initial_lng is None):
            raise ValueError(
                "initial_lat and initial_lng must both be set or both be null"
            )
        if self.initial_lat is not None and not (-90 <= self.initial_lat <= 90):
            raise ValueError("initial_lat must be in [-90, 90]")
        if self.initial_lng is not None and not (-180 <= self.initial_lng <= 180):
            raise ValueError("initial_lng must be in [-180, 180]")
        return self


# --------------------------------------------------------------------------
# Visit — response
# --------------------------------------------------------------------------
class VisitResponse(BaseModel):
    """Single visit with all joined display + nested children.

    Populated by `GET /visits/{id}/with-items` (staff + admin). The
    patient/guardian portal gets a slimmer shape via
    `PortalVisitResponse`. Mirrors the staff Visit Summary mockup:

      - patient card: patient_name, visit_date_label, time_range_label,
        duration_label
      - EVV record: evv_record (start + end + verification status)
      - activities: VisitActivityDelivery list (status + name +
        completed_time_label)
      - notes: VisitNote list (note_time_label)
      - signature: AppointmentSignature (signer_display_name, image, signed_at)
    """

    model_config = ConfigDict(from_attributes=True)

    # ---- raw columns (kept for backward compat with FE) ----
    id: UUID
    appointment_id: UUID
    agency_id: UUID
    staff_id: UUID
    status: VisitStatus
    billing_confirmed_at: datetime | None
    # Live GPS
    live_lat: Decimal | None
    live_lng: Decimal | None
    live_ping_at: datetime | None
    live_accuracy_m: Decimal | None
    sharing_location: bool
    created_at: datetime
    updated_at: datetime

    # ---- joined display fields (hydrated by _to_response) ----
    staff_name: str | None = None
    staff_role_label: str | None = None  # "DSP" suffix
    patient_name: str | None = None
    patient_initials: str | None = None  # "JS"
    program_name: str | None = None
    service_type_label: str | None = None
    location_label: str | None = None
    # ---- derived (computed from evv_record) ----
    duration_seconds: int | None = None
    duration_label: str | None = None  # "2h 05m"
    time_range_label: str | None = None  # "9:02 AM — 11:07 AM"
    visit_date_label: str | None = None  # "Tuesday, May 5, 2026"
    evv_passed: bool = False  # accuracy <= 100m threshold
    gps_verified: bool = False  # start_lat/lng both present

    # ---- nested (eager-loaded by load_visit_with_relations) ----
    evv_record: EVVRecordResponse | None = None
    activities: list[VisitActivityDeliveryResponse] | None = None
    notes: list[VisitNoteResponse] | None = None
    signature: AppointmentSignatureResponse | None = None


class VisitSummaryResponse(BaseModel):
    """Lighter shape for the live-monitor list endpoint.

    No nested children; only the joined fields the live EVV monitor
    needs to render one card per visit.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    appointment_id: UUID
    agency_id: UUID
    staff_id: UUID
    status: VisitStatus
    billing_confirmed_at: datetime | None
    live_lat: Decimal | None
    live_lng: Decimal | None
    live_ping_at: datetime | None
    live_accuracy_m: Decimal | None
    sharing_location: bool
    created_at: datetime
    updated_at: datetime
    # Joined display
    staff_name: str | None = None
    patient_name: str | None = None
    service_item_count: int = 0  # count of activities on the parent appt
    duration_label: str | None = None


# --------------------------------------------------------------------------
# EVV
# --------------------------------------------------------------------------
class EVVRecordResponse(BaseModel):
    """Electronic Visit Verification record (start + end)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    visit_id: UUID
    agency_id: UUID
    # Start
    start_time: datetime | None
    start_lat: Decimal | None
    start_lng: Decimal | None
    start_accuracy_m: Decimal | None
    start_device_id: str | None
    start_verification_status: str | None  # PENDING | VERIFIED | FAILED
    start_verified: bool = False  # derived: present + accuracy<=100m
    # End
    end_time: datetime | None
    end_lat: Decimal | None
    end_lng: Decimal | None
    end_accuracy_m: Decimal | None
    # Derived
    duration_seconds: int | None = None  # end_time - start_time


# --------------------------------------------------------------------------
# Activities
# --------------------------------------------------------------------------
class VisitActivityUpdateRequest(BaseModel):
    """PATCH /visits/{id}/activities/{activity_id} — record the outcome.

    Per spec §5, `NOT_DONE` requires a reason (DB-enforced).
    """

    model_config = ConfigDict(extra="forbid")

    status: ServiceItemStatus | None = None
    reason: Annotated[str, StringConstraints(max_length=4000)] | None = None
    note: Annotated[str, StringConstraints(max_length=4000)] | None = None

    @model_validator(mode="after")
    def _validate_not_done_has_reason(self) -> VisitActivityUpdateRequest:
        if self.status == ServiceItemStatus.NOT_DONE and (
            self.reason is None or not self.reason.strip()
        ):
            raise ValueError("reason is required when status = NOT_DONE")
        return self


class VisitActivityDeliveryResponse(BaseModel):
    """Per-visit delivery record for one parent activity.

    `name` is joined from `appointment_activities.name` (the free-text
    activity the admin typed at scheduling time — spec §2).
    `service_type_label` is `humanize_enum(name)` if name is enum-shaped,
    else None.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    visit_id: UUID
    activity_id: UUID
    status: ServiceItemStatus
    reason: str | None
    note: str | None
    completed_at: datetime | None
    completed_by: UUID | None
    created_at: datetime
    updated_at: datetime
    # Joined from parent activity
    name: str | None = None
    planned_minutes: int | None = None
    # Display labels (computed by _to_response)
    completed_time_label: str | None = None  # "10:24 AM"


# --------------------------------------------------------------------------
# Notes
# --------------------------------------------------------------------------
class VisitNoteCreateRequest(BaseModel):
    """POST /visits/{id}/notes — add a note."""

    model_config = ConfigDict(extra="forbid")

    body: Annotated[str, StringConstraints(min_length=1, max_length=10000)]

    @model_validator(mode="after")
    def _validate_body_non_empty(self) -> VisitNoteCreateRequest:
        if not self.body.strip():
            raise ValueError("body must not be empty or whitespace-only")
        return self


class VisitNoteResponse(BaseModel):
    """A note posted during the visit."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    visit_id: UUID
    author_user_id: UUID
    body: str
    created_at: datetime
    # Display labels
    note_time_label: str | None = None  # "9:35 AM"
    author_name: str | None = None  # joined from author.full_name


# --------------------------------------------------------------------------
# Signature
# --------------------------------------------------------------------------
class VisitSignRequest(BaseModel):
    """POST /visits/{id}/sign — file a signature (multipart in the router).

    Per spec §8-9, signing is mandatory. Signer may be PATIENT or
    GUARDIAN. `signer_role` is derived from the caller's role when the
    router authenticates; this body only carries the rendered display
    name override for legacy back-compat.
    """

    model_config = ConfigDict(extra="forbid")

    signer_display_name_override: (
        Annotated[str, StringConstraints(max_length=255)] | None
    ) = None  # only used if the FE has a custom rendering


class AppointmentSignatureResponse(BaseModel):
    """The required patient-or-guardian signature on the visit.

    `signer_display_name` follows the spec format `"J. Smith"` (first
    letter + last name) — see `signer_display_name()` in
    `src.shared.utils.labels`.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    visit_id: UUID
    agency_id: UUID
    signer_user_id: UUID
    signer_role: UserRole
    signer_display_name: str  # "J. Smith"
    signature_image_url: str
    signed_at: datetime
    ip_address: str | None
    user_agent: str | None
    created_at: datetime


# --------------------------------------------------------------------------
# Forward refs
# --------------------------------------------------------------------------
VisitResponse.model_rebuild()
AppointmentSignatureResponse.model_rebuild()
EVVRecordResponse.model_rebuild()


__all__ = [
    "AppointmentSignatureResponse",
    "EVVRecordResponse",
    "VisitActivityDeliveryResponse",
    "VisitActivityUpdateRequest",
    "VisitConfirmBillingRequest",
    "VisitCreateRequest",
    "VisitEndRequest",
    "VisitLocationPingRequest",
    "VisitNoteCreateRequest",
    "VisitNoteResponse",
    "VisitResponse",
    "VisitSignRequest",
    "VisitStartLocationSharingRequest",
    "VisitStatusTransitionRequest",
    "VisitSummaryResponse",
]
