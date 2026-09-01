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
from typing import Literal
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


# --------------------------------------------------------------------------
# Compliance dashboard (`GET /portal/compliance`)
# --------------------------------------------------------------------------
class PortalComplianceSubScore(BaseModel):
    """One row of the dashboard donut — Documentation / Staff Training /
    Service Auth. `color` mirrors the FE tailwind token directly so the
    widget can render without a remap."""

    model_config = ConfigDict(extra="forbid")

    key: Literal["documentation", "staff_training", "service_auth"]
    label: str
    percent: int  # 0-100
    color: Literal["green", "orange", "red"]


class PortalComplianceUrgentAction(BaseModel):
    """One card in the `Urgent Actions` widget."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    title: str
    description: str | None = None
    due_label: str  # human-readable ("2 days", "Overdue by 1 day")
    severity: Literal["critical", "high", "medium", "low"]


class PortalComplianceUpcomingAudit(BaseModel):
    """One card in the `Upcoming Audits` widget."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    title: str
    scheduled_for: datetime
    kind: Literal["document", "license", "issue"]


class PortalComplianceRecentActivity(BaseModel):
    """One row of the `Recent Activity` widget."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    actor_label: str
    description: str
    occurred_at: datetime


class PortalComplianceResponse(BaseModel):
    """Aggregate compliance dashboard payload for the patient/guardian.

    Mirrors the `/compliance` hub page in
    `farhan-salad-website/app/(dashboard)/compliance/page.tsx`. The score
    is computed on read from live counts (documents, licenses, issues)
    so the FE never sees stale aggregates.
    """

    model_config = ConfigDict(extra="forbid")

    overall_percent: int
    sub_scores: list[PortalComplianceSubScore]
    urgent_actions: list[PortalComplianceUrgentAction]
    upcoming_audits: list[PortalComplianceUpcomingAudit]
    recent_activity: list[PortalComplianceRecentActivity]
    generated_at: datetime


__all__ = [
    "PortalComplianceResponse",
    "PortalComplianceRecentActivity",
    "PortalComplianceSubScore",
    "PortalComplianceUpcomingAudit",
    "PortalComplianceUrgentAction",
    "PortalVisitListItem",
    "PortalVisitResponse",
]