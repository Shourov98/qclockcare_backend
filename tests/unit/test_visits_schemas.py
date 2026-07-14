"""Unit tests for visits Pydantic schemas.

Pure-Pydantic: no DB, no app. Validates field-level constraints and the
custom model_validators (lat/lng pair, NOT_DONE reason, DISPUTED reason).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.modules.visits.schemas import (
    ServiceVerificationCreateRequest,
    VisitCheckInRequest,
    VisitCreateRequest,
    VisitIssueCreateRequest,
    VisitNoteCreateRequest,
    VisitServiceItemUpdateRequest,
)
from src.shared.domain.enums import (
    DisputeReasonCode,
    ServiceItemStatus,
    VerificationStatus,
)

_UUID_A = "00000000-0000-0000-0000-000000000001"
_UUID_B = "00000000-0000-0000-0000-000000000002"


# --------------------------------------------------------------------------
# VisitCreateRequest
# --------------------------------------------------------------------------
class TestVisitCreateRequest:
    def test_minimal(self) -> None:
        req = VisitCreateRequest(appointment_id=_UUID_A)
        assert req.appointment_id == uuid.UUID(_UUID_A)
        assert req.check_in_lat is None
        assert req.check_in_lng is None

    def test_with_full_gps(self) -> None:
        req = VisitCreateRequest(
            appointment_id=_UUID_A,
            check_in_lat=Decimal("44.9778"),
            check_in_lng=Decimal("-93.2650"),
            check_in_accuracy_m=Decimal("5.00"),
            check_in_device_id="iphone-15-A",
            check_in_address_match=True,
            check_in_distance_from_location_m=Decimal("12.5"),
        )
        assert req.check_in_lat == Decimal("44.9778")
        assert req.check_in_address_match is True

    def test_lat_lng_pair_required(self) -> None:
        # Only lat, no lng
        with pytest.raises(ValidationError) as exc:
            VisitCreateRequest(
                appointment_id=_UUID_A,
                check_in_lat=Decimal("44.9778"),
            )
        assert "must both be set" in str(exc.value)

    def test_lng_without_lat_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VisitCreateRequest(
                appointment_id=_UUID_A,
                check_in_lng=Decimal("-93.2650"),
            )

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VisitCreateRequest(
                appointment_id=_UUID_A,
                extra_field="bogus",  # type: ignore[call-arg]
            )


# --------------------------------------------------------------------------
# VisitCheckInRequest
# --------------------------------------------------------------------------
class TestVisitCheckInRequest:
    def test_empty(self) -> None:
        req = VisitCheckInRequest()
        assert req.check_in_lat is None

    def test_partial_update(self) -> None:
        req = VisitCheckInRequest(check_in_device_id="pixel-8")
        assert req.check_in_device_id == "pixel-8"


# --------------------------------------------------------------------------
# VisitServiceItemUpdateRequest
# --------------------------------------------------------------------------
class TestVisitServiceItemUpdateRequest:
    def test_status_done_no_reason_required(self) -> None:
        req = VisitServiceItemUpdateRequest(status=ServiceItemStatus.DONE)
        assert req.status == ServiceItemStatus.DONE
        assert req.reason is None  # DONE doesn't require reason

    def test_status_not_done_requires_reason(self) -> None:
        with pytest.raises(ValidationError) as exc:
            VisitServiceItemUpdateRequest(status=ServiceItemStatus.NOT_DONE)
        assert "reason is required when status = NOT_DONE" in str(exc.value)

    def test_status_not_done_with_reason_ok(self) -> None:
        req = VisitServiceItemUpdateRequest(
            status=ServiceItemStatus.NOT_DONE,
            reason="Patient declined",
        )
        assert req.reason == "Patient declined"

    def test_status_not_done_with_whitespace_reason_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VisitServiceItemUpdateRequest(
                status=ServiceItemStatus.NOT_DONE,
                reason="   ",
            )

    def test_no_status_update_means_no_reason_required(self) -> None:
        req = VisitServiceItemUpdateRequest(note="Just a note")
        assert req.note == "Just a note"


# --------------------------------------------------------------------------
# ServiceVerificationCreateRequest
# --------------------------------------------------------------------------
class TestServiceVerificationCreateRequest:
    def test_verified_no_reason_required(self) -> None:
        req = ServiceVerificationCreateRequest(status=VerificationStatus.VERIFIED)
        assert req.status == VerificationStatus.VERIFIED
        assert req.dispute_reason_code is None

    def test_disputed_requires_reason(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ServiceVerificationCreateRequest(status=VerificationStatus.DISPUTED)
        assert "dispute_reason_code is required" in str(exc.value)

    def test_disputed_with_reason_ok(self) -> None:
        req = ServiceVerificationCreateRequest(
            status=VerificationStatus.DISPUTED,
            dispute_reason_code=DisputeReasonCode.STAFF_NEVER_ARRIVED,
            comment="Caregiver never showed up",
        )
        assert req.dispute_reason_code == DisputeReasonCode.STAFF_NEVER_ARRIVED


# --------------------------------------------------------------------------
# VisitNoteCreateRequest
# --------------------------------------------------------------------------
class TestVisitNoteCreateRequest:
    def test_minimal(self) -> None:
        req = VisitNoteCreateRequest(body="Patient was in good spirits.")
        assert req.body == "Patient was in good spirits."

    def test_empty_body_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VisitNoteCreateRequest(body="")

    def test_whitespace_only_body_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VisitNoteCreateRequest(body="   \n\t  ")


# --------------------------------------------------------------------------
# VisitIssueCreateRequest
# --------------------------------------------------------------------------
class TestVisitIssueCreateRequest:
    def test_minimal(self) -> None:
        req = VisitIssueCreateRequest(
            issue_type="noise_complaint",
            comment="Loud TV during visit",
        )
        assert req.issue_type == "noise_complaint"

    def test_empty_issue_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VisitIssueCreateRequest(issue_type="", comment="x")

    def test_empty_comment_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VisitIssueCreateRequest(issue_type="x", comment="")
