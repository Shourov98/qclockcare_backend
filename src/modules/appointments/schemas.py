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

from src.shared.domain.enums import (
    AppointmentEventType,
    AppointmentStatus,
    ConfirmationStatus,
    ProgramType,
    ServiceItemStatus,
    ServiceType,
    UserRole,
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
    notes: Annotated[str, StringConstraints(max_length=4000)] | None = None
    # Optional initial set of service items
    service_items: list[AppointmentServiceItemCreateRequest] = Field(
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
    """Generic status / confirmation update endpoint payload."""

    model_config = ConfigDict(extra="forbid")

    status: AppointmentStatus
    confirmation_status: ConfirmationStatus | None = None
    note: Annotated[str, StringConstraints(max_length=4000)] | None = None


class AppointmentCancelRequest(BaseModel):
    """POST /appointments/{id}/cancel — cancellation payload."""

    model_config = ConfigDict(extra="forbid")

    reason: Annotated[str, StringConstraints(min_length=1, max_length=4000)]


class AppointmentResponse(BaseModel):
    """Single appointment, optionally with nested service items."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    agency_id: UUID
    patient_id: UUID
    staff_id: UUID | None
    program_type: ProgramType | None
    scheduled_start: datetime
    scheduled_end: datetime
    status: AppointmentStatus
    confirmation_status: ConfirmationStatus | None
    confirmed_at: datetime | None
    confirmation_note: str | None
    checked_in_at: datetime | None
    checked_out_at: datetime | None
    completed_at: datetime | None
    location: str | None
    notes: str | None
    cancelled_reason: str | None
    cancelled_at: datetime | None
    created_at: datetime
    updated_at: datetime
    # Optional nested — populated only by GET /appointments/{id}/with-items
    service_items: list[AppointmentServiceItemResponse] | None = None


class AppointmentSummaryResponse(BaseModel):
    """Lighter shape for list endpoints — no nested service items."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    agency_id: UUID
    patient_id: UUID
    staff_id: UUID | None
    program_type: ProgramType | None
    scheduled_start: datetime
    scheduled_end: datetime
    status: AppointmentStatus
    confirmation_status: ConfirmationStatus | None
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------
# Service items
# --------------------------------------------------------------------------
class AppointmentServiceItemCreateRequest(BaseModel):
    """POST /appointments/{id}/service-items — add a service item."""

    model_config = ConfigDict(extra="forbid")

    service_type: ServiceType
    planned_minutes: Annotated[int, Field(gt=0, le=24 * 60)] | None = None
    notes: Annotated[str, StringConstraints(max_length=4000)] | None = None


class AppointmentServiceItemUpdateRequest(BaseModel):
    """PATCH /appointments/{id}/service-items/{item_id}."""

    model_config = ConfigDict(extra="forbid")

    service_type: ServiceType | None = None
    planned_minutes: Annotated[int, Field(gt=0, le=24 * 60)] | None = None
    status: ServiceItemStatus | None = None
    notes: Annotated[str, StringConstraints(max_length=4000)] | None = None


class AppointmentServiceItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    appointment_id: UUID
    agency_id: UUID
    service_type: ServiceType
    planned_minutes: int | None
    status: ServiceItemStatus
    notes: str | None
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------
# Lifecycle — confirm / request-reschedule / request-cancellation
# --------------------------------------------------------------------------
class AppointmentConfirmRequest(BaseModel):
    """POST /appointments/{id}/confirm — patient/guardian confirms or declines.

    `declined=True` records the confirmation with `status=DECLINED` (the
    appointment itself stays in its current state — admin must call
    `/cancel` to finalise). Default = confirmed.
    """

    model_config = ConfigDict(extra="forbid")

    declined: bool = False
    comment: Annotated[str, StringConstraints(max_length=4000)] | None = None


class AppointmentRescheduleRequest(BaseModel):
    """POST /appointments/{id}/request-reschedule — patient proposes a new window."""

    model_config = ConfigDict(extra="forbid")

    proposed_start: datetime
    proposed_end: datetime
    comment: Annotated[str, StringConstraints(max_length=4000)] | None = None

    @model_validator(mode="after")
    def _validate_window(self) -> AppointmentRescheduleRequest:
        if self.proposed_end <= self.proposed_start:
            raise ValueError("proposed_end must be after proposed_start")
        return self


class AppointmentCancellationRequest(BaseModel):
    """POST /appointments/{id}/request-cancellation — patient asks to cancel."""

    model_config = ConfigDict(extra="forbid")

    reason: Annotated[str, StringConstraints(min_length=1, max_length=4000)]


class AppointmentConfirmationResponse(BaseModel):
    """One confirmation row — returned by GET /confirmations and POST /confirm."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    appointment_id: UUID
    confirmed_by: UUID
    confirmation_role: UserRole
    status: ConfirmationStatus
    comment: str | None
    created_at: datetime


class AppointmentEventResponse(BaseModel):
    """Single event row — returned by GET /appointments/{id}/events."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    appointment_id: UUID
    agency_id: UUID
    actor_user_id: UUID | None
    event_type: AppointmentEventType
    from_status: AppointmentStatus | None
    to_status: AppointmentStatus | None
    metadata_: dict = Field(default_factory=dict, alias="metadata")
    ip_address: str | None
    user_agent: str | None
    created_at: datetime


# --------------------------------------------------------------------------
# Forward refs
# --------------------------------------------------------------------------
AppointmentCreateRequest.model_rebuild()
AppointmentResponse.model_rebuild()


__all__ = [
    "AppointmentCancelRequest",
    "AppointmentCancellationRequest",
    "AppointmentConfirmRequest",
    "AppointmentConfirmationResponse",
    "AppointmentCreateRequest",
    "AppointmentEventResponse",
    "AppointmentRescheduleRequest",
    "AppointmentResponse",
    "AppointmentServiceItemCreateRequest",
    "AppointmentServiceItemResponse",
    "AppointmentServiceItemUpdateRequest",
    "AppointmentStatusTransitionRequest",
    "AppointmentSummaryResponse",
    "AppointmentUpdateRequest",
]
