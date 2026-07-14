"""Patient/Guardian portal schemas — `/portal/visits/...` request/response shapes.

The portal is a read+verify surface for the patient or their linked
guardian. The shapes are kept narrow on purpose: the patient shouldn't
see internal admin fields (e.g. visit_service_items.completed_by) and
shouldn't be able to set internal status enums via the verify endpoint
(dedicated `verify` and `dispute` endpoints handle that explicitly).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints

from src.modules.visits.schemas import (
    ServiceVerificationResponse,
    VisitIssueResponse,
    VisitServiceItemResponse,
)


# --------------------------------------------------------------------------
# Response
# --------------------------------------------------------------------------
class PortalVisitResponse(BaseModel):
    """Single visit, scoped to the calling patient/guardian.

    Includes nested children so the portal can render the verify/dispute
    UI without a second round trip.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    appointment_id: UUID
    agency_id: UUID
    staff_id: UUID
    status: str
    check_in_time: datetime | None
    check_in_lat: Decimal | None
    check_in_lng: Decimal | None
    check_in_accuracy_m: Decimal | None
    check_in_address_match: bool | None
    check_in_distance_from_location_m: Decimal | None
    check_out_time: datetime | None
    check_out_lat: Decimal | None
    check_out_lng: Decimal | None
    duration_seconds: int | None
    created_at: datetime
    updated_at: datetime
    service_items: list[VisitServiceItemResponse] | None = None
    verification: ServiceVerificationResponse | None = None
    issues: list[VisitIssueResponse] | None = None


class PortalVisitListItem(BaseModel):
    """Lighter shape for the list endpoint."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    appointment_id: UUID
    status: str
    check_in_time: datetime | None
    check_out_time: datetime | None
    duration_seconds: int | None
    created_at: datetime


# --------------------------------------------------------------------------
# Request
# --------------------------------------------------------------------------
class PortalVerifyRequest(BaseModel):
    """POST /portal/visits/{id}/verify — confirm services were delivered.

    The portal can only file a positive verification through this
    endpoint. Filing a dispute goes through the dedicated `/dispute`
    endpoint so the reason is never forgotten.
    """

    model_config = ConfigDict(extra="forbid")

    comment: Annotated[str, StringConstraints(max_length=4000)] | None = None


class PortalDisputeRequest(BaseModel):
    """POST /portal/visits/{id}/dispute — mark services disputed.

    `dispute_reason_code` is required (it's also a DB-level CHECK).
    """

    model_config = ConfigDict(extra="forbid")

    dispute_reason_code: str  # validated against DisputeReasonCode at the service layer
    comment: Annotated[str, StringConstraints(max_length=4000)] | None = None


class PortalReportIssueRequest(BaseModel):
    """POST /portal/visits/{id}/report-issue — non-blocking issue report.

    Reuses the existing VisitIssueCreateRequest shape (issue_type +
    non-empty comment) so the service layer can call the same code path.
    """

    model_config = ConfigDict(extra="forbid")

    issue_type: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    comment: Annotated[str, StringConstraints(min_length=1, max_length=4000)]


__all__ = [
    "PortalDisputeRequest",
    "PortalReportIssueRequest",
    "PortalVerifyRequest",
    "PortalVisitListItem",
    "PortalVisitResponse",
]
