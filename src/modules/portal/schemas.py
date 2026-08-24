"""Patient/Guardian portal schemas — `/portal/visits/...` request/response shapes.

The portal is a read-only surface for the patient or their linked
guardian. State-mutating actions (signing, marking the visit) live on
the visits router (`POST /visits/{id}/sign` accepts PATIENT/GUARDIAN/
AGENCY_ADMIN per spec §8).

Shapes are kept narrow on purpose: the patient shouldn't see internal
admin fields (e.g. `visit_activity_deliveries.completed_by`) and
shouldn't be able to set internal status enums via the read endpoints.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from src.modules.visits.schemas import (
    AppointmentSignatureResponse,
    VisitActivityDeliveryResponse,
    VisitNoteResponse,
)


# --------------------------------------------------------------------------
# Response
# --------------------------------------------------------------------------
class PortalVisitResponse(BaseModel):
    """Single visit, scoped to the calling patient/guardian.

    Mirrors the staff Visit Summary shape but trimmed for portal use:
    shows the patient-friendly caregiver name + joined display labels,
    the EVV record (start + end), per-activity delivery status, notes,
    and the (required) signature when filed.

    Mirrors `VisitResponse` so the same FE visit-summary card renders
    correctly for both audiences, with the portal variant adding the
    patient-friendly `patient_name` join + dropping the internal admin
    fields the patient shouldn't see.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    appointment_id: UUID
    agency_id: UUID
    staff_id: UUID
    status: str
    billing_confirmed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    # ----- Joined caregiver (staff) info -----
    staff_name: str | None = None
    staff_phone: str | None = None
    staff_code: str | None = None
    staff_role_label: str | None = None
    # ----- Joined patient info (via visit.appointment.patient) -----
    patient_name: str | None = None
    patient_initials: str | None = None
    patient_code: str | None = None
    # ----- Free-text location from the parent appointment -----
    location_label: str | None = None
    # ----- Derived (computed by service) -----
    duration_seconds: int | None = None
    duration_label: str | None = None  # "2h 05m"
    time_range_label: str | None = None  # "9:02 AM — 11:07 AM"
    visit_date_label: str | None = None  # "Tuesday, May 5, 2026"
    # ----- Nested children (already eager-loaded by
    #       `portal_service.load_visit_with_relations`) -----
    activities: list[VisitActivityDeliveryResponse] | None = None
    notes: list[VisitNoteResponse] | None = None
    signature: AppointmentSignatureResponse | None = None
    # Live GPS — surfaced in portal too so the patient can see "your
    # caregiver is currently en route" / "has arrived" markers.
    live_lat: Decimal | None = None
    live_lng: Decimal | None = None
    live_ping_at: datetime | None = None
    sharing_location: bool = False


class PortalVisitListItem(BaseModel):
    """Lighter shape for the list endpoint."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    appointment_id: UUID
    status: str
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    duration_label: str | None = None
    staff_name: str | None = None
    created_at: datetime


__all__ = [
    "PortalVisitListItem",
    "PortalVisitResponse",
]