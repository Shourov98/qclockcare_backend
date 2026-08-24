"""Unit tests for visits Pydantic schemas.

Pure-Pydantic: no DB, no app. Validates field-level constraints and the
custom model_validators (lat/lng pair, NOT_DONE reason, etc.).

Per migration 0027 / spec alignment:
  - check_in/check_out fields replaced by start_lat/start_lng + EVVRecord
  - ServiceVerification replaced by AppointmentSignature (multipart upload,
    handled in the router not the schema)
  - VisitIssue removed (deferred work)
  - VisitServiceItemUpdateRequest renamed to VisitActivityUpdateRequest
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.modules.visits.schemas import (
    VisitActivityUpdateRequest,
    VisitConfirmBillingRequest,
    VisitCreateRequest,
    VisitEndRequest,
    VisitNoteCreateRequest,
    VisitSignRequest,
    VisitStatusTransitionRequest,
)
from src.shared.domain.enums import ServiceItemStatus, VisitStatus

_UUID_A = "00000000-0000-0000-0000-000000000001"
_UUID_B = "00000000-0000-0000-0000-000000000002"


# --------------------------------------------------------------------------
# VisitCreateRequest
# --------------------------------------------------------------------------
class TestVisitCreateRequest:
    def test_minimal(self) -> None:
        req = VisitCreateRequest(appointment_id=_UUID_A)
        assert req.appointment_id == uuid.UUID(_UUID_A)
        assert req.start_lat is None
        assert req.start_lng is None

    def test_with_full_gps(self) -> None:
        req = VisitCreateRequest(
            appointment_id=_UUID_A,
            start_lat=Decimal("44.9778"),
            start_lng=Decimal("-93.2650"),
            start_accuracy_m=Decimal("5.00"),
            start_device_id="iphone-15-A",
        )
        assert req.start_lat == Decimal("44.9778")
        assert req.start_device_id == "iphone-15-A"

    def test_lat_lng_pair_required(self) -> None:
        # Only lat, no lng
        with pytest.raises(ValidationError) as exc:
            VisitCreateRequest(
                appointment_id=_UUID_A,
                start_lat=Decimal("44.9778"),
            )
        assert "must both be set" in str(exc.value)

    def test_lng_without_lat_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VisitCreateRequest(
                appointment_id=_UUID_A,
                start_lng=Decimal("-93.2650"),
            )

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VisitCreateRequest(
                appointment_id=_UUID_A,
                extra_field="bogus",  # type: ignore[call-arg]
            )


# --------------------------------------------------------------------------
# VisitEndRequest
# --------------------------------------------------------------------------
class TestVisitEndRequest:
    def test_minimal(self) -> None:
        req = VisitEndRequest()
        assert req.end_lat is None
        assert req.end_lng is None
        assert req.end_accuracy_m is None

    def test_with_end_gps(self) -> None:
        req = VisitEndRequest(
            end_lat=Decimal("44.9778"),
            end_lng=Decimal("-93.2650"),
            end_accuracy_m=Decimal("6.50"),
        )
        assert req.end_lat == Decimal("44.9778")
        assert req.end_accuracy_m == Decimal("6.50")


# --------------------------------------------------------------------------
# VisitConfirmBillingRequest
# --------------------------------------------------------------------------
class TestVisitConfirmBillingRequest:
    def test_empty_payload(self) -> None:
        # No fields — POST just confirms the box was ticked.
        req = VisitConfirmBillingRequest()
        assert req.model_dump() == {}


# --------------------------------------------------------------------------
# VisitStatusTransitionRequest
# --------------------------------------------------------------------------
class TestVisitStatusTransitionRequest:
    def test_status_required(self) -> None:
        req = VisitStatusTransitionRequest(status=VisitStatus.IN_PROGRESS)
        assert req.status == VisitStatus.IN_PROGRESS

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VisitStatusTransitionRequest(  # type: ignore[call-arg]
                status=VisitStatus.IN_PROGRESS,
                extra_field="bogus",
            )


# --------------------------------------------------------------------------
# VisitActivityUpdateRequest (renamed from VisitServiceItemUpdateRequest)
# --------------------------------------------------------------------------
class TestVisitActivityUpdateRequest:
    def test_status_done_no_reason_required(self) -> None:
        req = VisitActivityUpdateRequest(status=ServiceItemStatus.DONE)
        assert req.status == ServiceItemStatus.DONE
        assert req.reason is None  # DONE doesn't require reason

    def test_status_not_done_requires_reason(self) -> None:
        with pytest.raises(ValidationError) as exc:
            VisitActivityUpdateRequest(status=ServiceItemStatus.NOT_DONE)
        assert "reason is required when status = NOT_DONE" in str(exc.value)

    def test_status_not_done_with_reason_ok(self) -> None:
        req = VisitActivityUpdateRequest(
            status=ServiceItemStatus.NOT_DONE,
            reason="Patient declined",
        )
        assert req.reason == "Patient declined"

    def test_status_not_done_with_whitespace_reason_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VisitActivityUpdateRequest(
                status=ServiceItemStatus.NOT_DONE,
                reason="   ",
            )

    def test_no_status_update_means_no_reason_required(self) -> None:
        req = VisitActivityUpdateRequest(note="Just a note")
        assert req.note == "Just a note"


# --------------------------------------------------------------------------
# VisitSignRequest
# --------------------------------------------------------------------------
class TestVisitSignRequest:
    def test_minimal(self) -> None:
        req = VisitSignRequest()
        assert req.signer_display_name_override is None

    def test_with_display_name_override(self) -> None:
        req = VisitSignRequest(signer_display_name_override="J. Smith")
        assert req.signer_display_name_override == "J. Smith"

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VisitSignRequest.model_validate(
                {"signer_display_name_override": None, "junk": True}
            )


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
