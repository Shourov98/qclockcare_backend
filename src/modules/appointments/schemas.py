"""Appointments module — request/response Pydantic schemas (DTOs).

Wire format for every appointment + service-item endpoint.

Pattern:
- `*Request`  — what the client sends
- `*Response` — what we return
- Nested `*Nested` — child resources inlined in a parent response

See `13_DATABASE_SCHEMA_COMPLETE.md` §8 for the data model and
status lifecycle.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from src.modules.visits.schemas import AppointmentSignatureResponse
from src.shared.domain.enums import (
    AppointmentStatus,
    ProgramType,
    ServiceItemStatus,
)


# --------------------------------------------------------------------------
# Appointment
# --------------------------------------------------------------------------
class AppointmentCreateRequest(BaseModel):
    """POST /appointments — schedule a new visit."""

    model_config = ConfigDict(extra="forbid")

    patient_id: UUID
    staff_id: UUID | None = None
    program_type: ProgramType | None = None
    scheduled_start: datetime
    scheduled_end: datetime
    location: Annotated[str, StringConstraints(max_length=512)] | None = None
    # Optional structured location — points to a row in the `locations`
    # table which carries lat/lng + structured address. Both `location`
    # and `location_id` can be set (e.g. agency title in `location`,
    # FK in `location_id`) or only one. New appointments should prefer
    # `location_id` so the FE can render a map pin.
    location_id: UUID | None = None
    notes: Annotated[str, StringConstraints(max_length=4000)] | None = None
    # Optional initial set of activities (free-text per spec §2)
    activities: list[AppointmentActivityCreateRequest] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def _validate_window(self) -> AppointmentCreateRequest:
        if self.scheduled_end <= self.scheduled_start:
            raise ValueError("scheduled_end must be after scheduled_start")
        return self


class AppointmentUpdateRequest(BaseModel):
    """PATCH /appointments/{id} — partial update.

    Only fields explicitly set are applied. Status transitions are
    handled by dedicated endpoints (cancel, assign, check-in, etc.)
    so this stays narrow.
    """

    model_config = ConfigDict(extra="forbid")

    staff_id: UUID | None = None
    program_type: ProgramType | None = None
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    location: Annotated[str, StringConstraints(max_length=512)] | None = None
    # Use `None` to leave unchanged; send a UUID to set/replace; the
    # FE doesn't currently have a way to clear an existing FK — if
    # that becomes a need, we'd switch to a sentinel pattern.
    location_id: UUID | None = None
    notes: Annotated[str, StringConstraints(max_length=4000)] | None = None

    @model_validator(mode="after")
    def _validate_window(self) -> AppointmentUpdateRequest:
        if (
            self.scheduled_start is not None
            and self.scheduled_end is not None
            and self.scheduled_end <= self.scheduled_start
        ):
            raise ValueError("scheduled_end must be after scheduled_start")
        return self


class AppointmentStatusTransitionRequest(BaseModel):
    """Generic status update endpoint payload."""

    model_config = ConfigDict(extra="forbid")

    status: AppointmentStatus
    note: Annotated[str, StringConstraints(max_length=4000)] | None = None


class AppointmentResponse(BaseModel):
    """Single appointment, optionally with nested activities."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    agency_id: UUID
    patient_id: UUID
    staff_id: UUID | None
    program_type: ProgramType | None
    scheduled_start: datetime
    scheduled_end: datetime
    status: AppointmentStatus
    location: str | None
    notes: str | None
    cancelled_reason: str | None
    cancelled_at: datetime | None
    created_at: datetime
    updated_at: datetime
    # Structured location FK — same as the summary response. Surfaced
    # on the full response too so admin detail pages can show the map
    # pin without needing the summary endpoint.
    location_id: UUID | None = None
    latitude: float | None = None
    longitude: float | None = None
    location_name: str | None = None
    location_address: str | None = None
    # Optional nested — populated only by GET /appointments/{id}/with-items
    activities: list[AppointmentActivityResponse] | None = None
    # Patient-or-guardian signature captured at the visit level. The
    # 1:1 chain is `appointment → visit → signature`, so this is `null`
    # until the caregiver starts the visit. Populated alongside
    # `activities` on the with-items endpoint.
    signature: AppointmentSignatureResponse | None = None


class AppointmentSummaryResponse(BaseModel):
    """Lighter shape for list endpoints — eagerly joins caregiver name,
    staff phone, program label, first activity name, and the free-text
    location so the patient mobile app can render a fully populated
    card in a single round trip.

    All joined fields are nullable because the underlying relations
    may not exist (e.g. an unassigned `SCHEDULED` appointment has no
    staff). The FE should treat them as optional.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    agency_id: UUID
    patient_id: UUID
    staff_id: UUID | None
    program_type: ProgramType | None
    scheduled_start: datetime
    scheduled_end: datetime
    status: AppointmentStatus
    created_at: datetime
    updated_at: datetime
    # ----- Joined caregiver info -----
    # Populated from `Appointment.staff.user` and `Appointment.staff`.
    staff_name: str | None = None
    staff_phone: str | None = None
    staff_code: str | None = None
    # First non-expired qualification label (e.g. "CNA", "LPN"). Useful
    # for the patient-facing card so the family knows the caregiver's
    # credential before they arrive.
    staff_credential: str | None = None
    # ----- Joined program + service info -----
    # `program_name` is a human-readable rendering of the `program_type`
    # enum ("CFSS"); `service_type_label` is the first service item's
    # type rendered ("Personal Care"). Both join via
    # `Appointment.service_items`.
    program_name: str | None = None
    service_type_label: str | None = None
    # ----- Free-text location entered at scheduling -----
    # Already exists on the full `AppointmentResponse`; promoted to the
    # summary so the patient mobile app doesn't need a second round trip
    # to display "123 Oak St, Saint Paul MN".
    location_label: str | None = None
    # ----- Structured location (the `locations` row this appointment points to) -----
    # Lets the FE render a map pin without a second round trip. Populated
    # by `_summarize_to_dict` when `Appointment.location_rel` is
    # eagerly loaded via `selectinload`. All three are nullable because
    # the appointment may not be linked to a `locations` row.
    location_id: UUID | None = None
    latitude: float | None = None
    longitude: float | None = None
    location_name: str | None = None
    location_address: str | None = None
    # ----- Patient identity (header card) -----
    # `patient_name` is the full name; `patient_initials` is the avatar
    # initials ("JS"); `patient_code` is the per-agency patient code
    # ("PAT-001"). All three are needed so the patient app renders the
    # card with one round trip.
    patient_name: str | None = None
    patient_initials: str | None = None
    patient_code: str | None = None
    # ----- Duration label -----
    # Computed: planned (`scheduled_end - scheduled_start`) when no
    # visit yet, actual (from `evv_record`) once one exists. Renders as
    # "1h 00m" or "45m" via `duration_label()`.
    duration_label: str | None = None
    # ----- Billing (denormalized onto the appointment) -----
    # `billing_status` is `unpaid | paid`. `billing_paid_at` is the
    # timestamp of the staff/caregiver confirmation; `claim_id` is the
    # externally-rendered identifier (CG-{agency}-{appt}).
    billing_status: str = "unpaid"
    billing_paid_at: datetime | None = None
    claim_id: str | None = None


# --------------------------------------------------------------------------
# Activity items (renamed from service items)
# --------------------------------------------------------------------------
class AppointmentActivityCreateRequest(BaseModel):
    """POST /appointments/{id}/activities — add a free-text activity.

    Per spec §2, activities are free-text names entered by the admin at
    scheduling time ("Check blood pressure", "Prepare meal", etc.). The
    legacy `service_type` enum is replaced by the `name` string.
    """

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    planned_minutes: Annotated[int, Field(gt=0, le=24 * 60)] | None = None
    notes: Annotated[str, StringConstraints(max_length=4000)] | None = None


class AppointmentActivityUpdateRequest(BaseModel):
    """PATCH /appointments/{id}/activities/{activity_id}."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[
        str, StringConstraints(min_length=1, max_length=255)
    ] | None = None
    planned_minutes: Annotated[int, Field(gt=0, le=24 * 60)] | None = None
    status: ServiceItemStatus | None = None
    notes: Annotated[str, StringConstraints(max_length=4000)] | None = None


class AppointmentActivityResponse(BaseModel):
    """One activity row (admin view, no delivery record).

    `completed_at` / `completed_by_user_id` are stored on the row for the
    audit trail but intentionally not surfaced here — the activity card
    only renders the name, planned minutes, status, and notes.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    appointment_id: UUID
    agency_id: UUID
    name: str
    planned_minutes: int | None
    status: ServiceItemStatus
    notes: str | None
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------
# Lifecycle — ready (admin marks appointment ready for the caregiver)
# --------------------------------------------------------------------------
class AppointmentReadyRequest(BaseModel):
    """POST /appointments/{id}/ready — admin marks the appointment as
    ready for the assigned caregiver to start the visit.

    Transitions `SCHEDULED → READY`. The caregiver is then expected to
    call `POST /visits` to actually start the visit (which transitions
    `READY → IN_PROGRESS`).
    """

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Lifecycle — cancel
# --------------------------------------------------------------------------
class AppointmentCancelRequest(BaseModel):
    """POST /appointments/{id}/cancel — cancellation payload."""

    model_config = ConfigDict(extra="forbid")

    reason: Annotated[str, StringConstraints(min_length=1, max_length=4000)]


# --------------------------------------------------------------------------
# Lifecycle — missed (exception edge)
# --------------------------------------------------------------------------
class AppointmentMissedRequest(BaseModel):
    """POST /appointments/{id}/missed — admin marks an appointment missed.

    Distinct from `CANCELLED`: a MISSED appointment is one where the
    caregiver was a no-show (or the patient was unavailable without
    prior notice). The reason is required for audit and FE display.
    """

    model_config = ConfigDict(extra="forbid")

    reason: Annotated[str, StringConstraints(min_length=1, max_length=4000)]


# --------------------------------------------------------------------------
# Lifecycle — rejected (exception edge)
# --------------------------------------------------------------------------
class AppointmentRejectedRequest(BaseModel):
    """POST /appointments/{id}/rejected — patient/guardian rejected the
    appointment before it was performed.

    Distinct from `CANCELLED` (which is an admin-side action): a REJECTED
    appointment is one where the patient or guardian actively declined.
    """

    model_config = ConfigDict(extra="forbid")

    reason: Annotated[str, StringConstraints(min_length=1, max_length=4000)]


# --------------------------------------------------------------------------
# Lifecycle — billing confirmation
# --------------------------------------------------------------------------
class AppointmentMarkBillingPaidRequest(BaseModel):
    """POST /appointments/{id}/billing/paid — staff/admin flips the
    billing toggle to `paid`. No payload needed (the timestamp + actor
    are derived server-side).

    Kept as a typed model so future fields (e.g. payment reference)
    can be added without a breaking change.
    """

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Patient dashboard summary (returned by
# GET /patients/{id}/dashboard-summary)
# --------------------------------------------------------------------------
class PatientLifetimeStats(BaseModel):
    """Patient's lifetime service statistics.

    Returned by the patient dashboard summary endpoint. All numbers
    are scoped to the caller's agency (RLS-enforced) and computed
    on the server at request time — the FE doesn't need to
    aggregate.

    `total_service_minutes` is the sum of
    `(scheduled_end - scheduled_start)` in minutes for every
    appointment whose status is `COMPLETED`. We use the scheduled
    window (not EVV-derived durations) because:

      1. The EVV end call (`POST /visits/{id}/end`) is optional in
         the lifecycle — many completed visits never have an
         `evv_records.end_time`.
      2. The scheduled duration is what was actually billed to the
         payer, so it matches the patient-visible invoice.
      3. It's always populated (every appointment has
         `scheduled_start` and `scheduled_end`).

    `completed_visits` mirrors `completed_appointments` because
    the `appointment → visit` chain is 1:1 (the unique constraint
    on `visits.appointment_id`), but the FE might want to display
    them separately in the future.
    """

    model_config = ConfigDict(extra="forbid")

    total_appointments: int = Field(
        ge=0,
        description="Every appointment row for this patient at this agency, regardless of status.",
    )
    completed_appointments: int = Field(
        ge=0,
        description="Appointments with status=COMPLETED — i.e. the visit was signed off.",
    )
    completed_visits: int = Field(
        ge=0,
        description="Visits with status=COMPLETED. Equal to `completed_appointments` for the 1:1 chain.",
    )
    total_service_minutes: int = Field(
        ge=0,
        description="Sum of (scheduled_end - scheduled_start) in minutes across COMPLETED appointments.",
    )


class PatientDashboardSummaryResponse(BaseModel):
    """Aggregated data for the patient dashboard landing screen.

    Two halves:
      - `upcoming`  — next ≤5 appointments, scheduled_start >= now,
                     status in the `APPOINTMENT_ACTIVE_STATUSES`
                     set, ordered ascending by scheduled_start.
                     Empty list if the patient has nothing booked
                     in the near future.
      - `lifetime`  — counts + total minutes of service received
                     across all history.

    The upcoming list reuses `AppointmentSummaryResponse` so the
    patient FE doesn't need a separate type for dashboard cards.
    """

    model_config = ConfigDict(extra="forbid")

    upcoming: list[AppointmentSummaryResponse]
    lifetime: PatientLifetimeStats


class CalendarAppointmentsResponse(BaseModel):
    """Bucketed appointments for the FE's calendar UI.

    Used by `/me/appointments/calendar/grouped`. The three buckets
    partition the window returned by `list_appointments_in_window`
    (which itself is constrained by `date_from` / `date_to`):

      - `today`     — appointments whose scheduled_start falls on
                      the current calendar day in UTC.
      - `upcoming`  — appointments after today (within the window).
      - `past`      — appointments before today (within the window).

    Each item reuses `AppointmentSummaryResponse` so the FE doesn't
    need a separate card type for calendar cells.
    """

    model_config = ConfigDict(extra="forbid")

    today: list[AppointmentSummaryResponse] = Field(default_factory=list)
    upcoming: list[AppointmentSummaryResponse] = Field(default_factory=list)
    past: list[AppointmentSummaryResponse] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Forward refs
# --------------------------------------------------------------------------
AppointmentCreateRequest.model_rebuild()
AppointmentResponse.model_rebuild()
PatientDashboardSummaryResponse.model_rebuild()
CalendarAppointmentsResponse.model_rebuild()


__all__ = [
    "AppointmentActivityCreateRequest",
    "AppointmentActivityResponse",
    "AppointmentActivityUpdateRequest",
    "AppointmentCancelRequest",
    "AppointmentCreateRequest",
    "AppointmentMarkBillingPaidRequest",
    "AppointmentMissedRequest",
    "AppointmentReadyRequest",
    "AppointmentRejectedRequest",
    "AppointmentResponse",
    "AppointmentStatusTransitionRequest",
    "AppointmentSummaryResponse",
    "AppointmentUpdateRequest",
    "PatientDashboardSummaryResponse",
    "PatientLifetimeStats",
]
