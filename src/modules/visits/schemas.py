"""Visits module — request/response Pydantic schemas (DTOs).

Wire format for every visit + service-item + note + verification + issue
endpoint.

Pattern:
- `*Request`  — what the client sends
- `*Response` — what we return
- Nested `*Nested` — child resources inlined in a parent response

See `13_DATABASE_SCHEMA_COMPLETE.md` §11 and §12 for the data model.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

from src.shared.domain.enums import (
    DisputeReasonCode,
    ServiceItemStatus,
    UserRole,
    VerificationStatus,
    VisitStatus,
)


# --------------------------------------------------------------------------
# Visit
# --------------------------------------------------------------------------
class VisitCreateRequest(BaseModel):
    """POST /visits — create a visit (typically auto-created on check-in).

    In the typical flow, the staff app POSTs /visits with appointment_id
    + the GPS / device info captured at check-in. The service stamps
    check_in_time = now() and sets status = CHECKED_IN.
    """

    model_config = ConfigDict(extra="forbid")

    appointment_id: UUID
    check_in_lat: Decimal | None = None
    check_in_lng: Decimal | None = None
    check_in_accuracy_m: Decimal | None = None
    check_in_device_id: Annotated[str, StringConstraints(max_length=512)] | None = None
    check_in_address_match: bool | None = None
    check_in_distance_from_location_m: Decimal | None = None

    @model_validator(mode="after")
    def _validate_lat_lng_pair(self) -> VisitCreateRequest:
        if (self.check_in_lat is None) != (self.check_in_lng is None):
            raise ValueError("check_in_lat and check_in_lng must both be set or both be null")
        return self


class VisitCheckInRequest(BaseModel):
    """PATCH /visits/{id}/check-in — record the actual check-in.

    Used when the visit row was pre-created (e.g. by a "scheduled visit"
    endpoint) and the staff member is now actually at the location.
    """

    model_config = ConfigDict(extra="forbid")

    check_in_lat: Decimal | None = None
    check_in_lng: Decimal | None = None
    check_in_accuracy_m: Decimal | None = None
    check_in_device_id: Annotated[str, StringConstraints(max_length=512)] | None = None
    check_in_address_match: bool | None = None
    check_in_distance_from_location_m: Decimal | None = None


class VisitCheckOutRequest(BaseModel):
    """PATCH /visits/{id}/check-out — record the actual check-out."""

    model_config = ConfigDict(extra="forbid")

    check_out_lat: Decimal | None = None
    check_out_lng: Decimal | None = None
    check_out_accuracy_m: Decimal | None = None
    note: Annotated[str, StringConstraints(max_length=4000)] | None = None


class VisitStatusTransitionRequest(BaseModel):
    """PATCH /visits/{id}/transition — IN_PROGRESS / COMPLETED."""

    model_config = ConfigDict(extra="forbid")

    status: VisitStatus


class VisitResponse(BaseModel):
    """Single visit, optionally with nested service items / notes / issues."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    appointment_id: UUID
    agency_id: UUID
    staff_id: UUID
    status: VisitStatus
    check_in_time: datetime | None
    check_in_lat: Decimal | None
    check_in_lng: Decimal | None
    check_in_accuracy_m: Decimal | None
    check_in_device_id: str | None
    check_in_address_match: bool | None
    check_in_distance_from_location_m: Decimal | None
    check_out_time: datetime | None
    check_out_lat: Decimal | None
    check_out_lng: Decimal | None
    check_out_accuracy_m: Decimal | None
    duration_seconds: int | None
    created_at: datetime
    updated_at: datetime
    # Optional nested — populated only by GET /visits/{id}/with-items
    service_items: list[VisitServiceItemResponse] | None = None
    notes: list[VisitNoteResponse] | None = None
    verification: ServiceVerificationResponse | None = None
    issues: list[VisitIssueResponse] | None = None


class VisitSummaryResponse(BaseModel):
    """Lighter shape for list endpoints — no nested children."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    appointment_id: UUID
    agency_id: UUID
    staff_id: UUID
    status: VisitStatus
    check_in_time: datetime | None
    check_out_time: datetime | None
    duration_seconds: int | None
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------
# Visit service items
# --------------------------------------------------------------------------
class VisitServiceItemCreateRequest(BaseModel):
    """POST /visits/{id}/service-items — attach an appointment_service_item.

    On creation the row is PENDING. Staff then PATCH /{item_id} to mark
    it DONE / NOT_DONE / etc.
    """

    model_config = ConfigDict(extra="forbid")

    appointment_service_item_id: UUID
    note: Annotated[str, StringConstraints(max_length=4000)] | None = None


class VisitServiceItemUpdateRequest(BaseModel):
    """PATCH /visits/{id}/service-items/{item_id} — update delivery status.

    When `status` is NOT_DONE, `reason` becomes required — enforced by
    the service layer (DB also has a CHECK constraint).
    """

    model_config = ConfigDict(extra="forbid")

    status: ServiceItemStatus | None = None
    reason: Annotated[str, StringConstraints(max_length=4000)] | None = None
    note: Annotated[str, StringConstraints(max_length=4000)] | None = None

    @model_validator(mode="after")
    def _validate_not_done_has_reason(self) -> VisitServiceItemUpdateRequest:
        if self.status == ServiceItemStatus.NOT_DONE and (
            self.reason is None or not self.reason.strip()
        ):
            raise ValueError("reason is required when status = NOT_DONE")
        return self


class VisitServiceItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    visit_id: UUID
    appointment_service_item_id: UUID
    status: ServiceItemStatus
    reason: str | None
    note: str | None
    completed_at: datetime | None
    completed_by: UUID | None
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------
# Visit notes
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
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    visit_id: UUID
    author_user_id: UUID
    body: str
    created_at: datetime


# --------------------------------------------------------------------------
# Service verifications
# --------------------------------------------------------------------------
class ServiceVerificationCreateRequest(BaseModel):
    """POST /visits/{id}/verify — patient or guardian verifies the visit.

    When `status` is DISPUTED, `dispute_reason_code` is required.
    """

    model_config = ConfigDict(extra="forbid")

    status: VerificationStatus
    dispute_reason_code: DisputeReasonCode | None = None
    comment: Annotated[str, StringConstraints(max_length=4000)] | None = None

    @model_validator(mode="after")
    def _validate_dispute_has_reason(self) -> ServiceVerificationCreateRequest:
        if (
            self.status == VerificationStatus.DISPUTED
            and self.dispute_reason_code is None
        ):
            raise ValueError("dispute_reason_code is required when status = DISPUTED")
        return self


class ServiceVerificationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    visit_id: UUID
    agency_id: UUID
    verified_by: UUID
    verifier_role: UserRole
    status: VerificationStatus
    dispute_reason_code: DisputeReasonCode | None
    comment: str | None
    created_at: datetime


# --------------------------------------------------------------------------
# Visit issues
# --------------------------------------------------------------------------
class VisitIssueCreateRequest(BaseModel):
    """POST /visits/{id}/issues — file a non-blocking issue.

    Free-form `issue_type` (e.g. "noise_complaint", "late_arrival") +
    required `comment`. The DB CHECK constraints enforce non-empty
    strings; we mirror them here with min_length.
    """

    model_config = ConfigDict(extra="forbid")

    issue_type: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    comment: Annotated[str, StringConstraints(min_length=1, max_length=4000)]


class VisitIssueResolveRequest(BaseModel):
    """PATCH /visits/{id}/issues/{issue_id}/resolve — mark an issue resolved."""

    model_config = ConfigDict(extra="forbid")

    resolution_note: Annotated[str, StringConstraints(min_length=1, max_length=4000)]


class VisitIssueResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    visit_id: UUID
    agency_id: UUID
    reported_by: UUID
    issue_type: str
    comment: str
    resolved_at: datetime | None
    resolved_by: UUID | None
    resolution_note: str | None
    created_at: datetime


# --------------------------------------------------------------------------
# Forward refs
# --------------------------------------------------------------------------
VisitResponse.model_rebuild()


__all__ = [
    "ServiceVerificationCreateRequest",
    "ServiceVerificationResponse",
    "VisitCheckInRequest",
    "VisitCheckOutRequest",
    "VisitCreateRequest",
    "VisitIssueCreateRequest",
    "VisitIssueResolveRequest",
    "VisitIssueResponse",
    "VisitNoteCreateRequest",
    "VisitNoteResponse",
    "VisitResponse",
    "VisitServiceItemCreateRequest",
    "VisitServiceItemResponse",
    "VisitServiceItemUpdateRequest",
    "VisitStatusTransitionRequest",
    "VisitSummaryResponse",
]
