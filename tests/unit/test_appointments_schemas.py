"""Unit tests for appointments + service items Pydantic schemas.

Pure-Pydantic: no DB, no app. Validates field-level constraints and the
custom model_validators (window ordering, planned_minutes range, etc.).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from src.modules.appointments.schemas import (
    AppointmentCancelRequest,
    AppointmentCreateRequest,
    AppointmentServiceItemCreateRequest,
    AppointmentServiceItemUpdateRequest,
    AppointmentStatusTransitionRequest,
    AppointmentUpdateRequest,
)
from src.shared.domain.enums import (
    AppointmentStatus,
    ConfirmationStatus,
    ProgramType,
    ServiceItemStatus,
    ServiceType,
)

_UUID_A = "00000000-0000-0000-0000-000000000001"
_UUID_B = "00000000-0000-0000-0000-000000000002"

_START = datetime(2026, 7, 1, 9, 0, 0, tzinfo=UTC)
_END = _START + timedelta(hours=1)


# --------------------------------------------------------------------------
# AppointmentCreateRequest
# --------------------------------------------------------------------------
class TestAppointmentCreateRequest:
    def test_minimal_required_fields(self) -> None:
        req = AppointmentCreateRequest(
            patient_id=_UUID_A,
            scheduled_start=_START,
            scheduled_end=_END,
        )
        assert req.patient_id == uuid.UUID(_UUID_A)
        assert req.staff_id is None
        assert req.program_type is None
        assert req.service_items == []

    def test_all_fields(self) -> None:
        req = AppointmentCreateRequest(
            patient_id=_UUID_A,
            staff_id=_UUID_B,
            program_type=ProgramType.ARMHS,
            scheduled_start=_START,
            scheduled_end=_END,
            location="123 Main St",
            notes="Bring paperwork",
            service_items=[
                AppointmentServiceItemCreateRequest(
                    service_type=ServiceType.PERSONAL_CARE,
                    planned_minutes=60,
                    notes="First visit",
                ),
            ],
        )
        assert req.staff_id == uuid.UUID(_UUID_B)
        assert req.program_type == ProgramType.ARMHS
        assert len(req.service_items) == 1
        assert req.service_items[0].service_type == ServiceType.PERSONAL_CARE

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AppointmentCreateRequest(
                patient_id=_UUID_A,
                scheduled_start=_START,
                scheduled_end=_END,
                extra_field="bogus",  # type: ignore[call-arg]
            )

    def test_window_end_before_start_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            AppointmentCreateRequest(
                patient_id=_UUID_A,
                scheduled_start=_END,
                scheduled_end=_START,
            )
        assert "scheduled_end must be after scheduled_start" in str(exc.value)

    def test_window_end_equal_start_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AppointmentCreateRequest(
                patient_id=_UUID_A,
                scheduled_start=_START,
                scheduled_end=_START,
            )


# --------------------------------------------------------------------------
# AppointmentUpdateRequest
# --------------------------------------------------------------------------
class TestAppointmentUpdateRequest:
    def test_empty_update(self) -> None:
        req = AppointmentUpdateRequest()
        assert req.staff_id is None
        assert req.scheduled_start is None

    def test_partial_update(self) -> None:
        req = AppointmentUpdateRequest(
            staff_id=_UUID_B,
            location="Office",
        )
        assert req.staff_id == uuid.UUID(_UUID_B)
        assert req.location == "Office"

    def test_window_end_before_start_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AppointmentUpdateRequest(
                scheduled_start=_END,
                scheduled_end=_START,
            )

    def test_partial_window_does_not_require_both(self) -> None:
        # Only updating start should NOT require end to also be set.
        req = AppointmentUpdateRequest(scheduled_start=_START)
        assert req.scheduled_start == _START
        assert req.scheduled_end is None


# --------------------------------------------------------------------------
# AppointmentStatusTransitionRequest
# --------------------------------------------------------------------------
class TestAppointmentStatusTransitionRequest:
    def test_status_required(self) -> None:
        req = AppointmentStatusTransitionRequest(status=AppointmentStatus.SCHEDULED)
        assert req.status == AppointmentStatus.SCHEDULED
        assert req.confirmation_status is None
        assert req.note is None

    def test_with_confirmation(self) -> None:
        req = AppointmentStatusTransitionRequest(
            status=AppointmentStatus.CONFIRMED,
            confirmation_status=ConfirmationStatus.CONFIRMED,
            note="Patient confirmed by phone",
        )
        assert req.confirmation_status == ConfirmationStatus.CONFIRMED
        assert req.note == "Patient confirmed by phone"


# --------------------------------------------------------------------------
# AppointmentCancelRequest
# --------------------------------------------------------------------------
class TestAppointmentCancelRequest:
    def test_reason_required(self) -> None:
        with pytest.raises(ValidationError):
            AppointmentCancelRequest(reason="")  # type: ignore[arg-type]

    def test_min_length_one(self) -> None:
        req = AppointmentCancelRequest(reason="Cancelled by patient")
        assert req.reason == "Cancelled by patient"


# --------------------------------------------------------------------------
# AppointmentServiceItemCreateRequest
# --------------------------------------------------------------------------
class TestAppointmentServiceItemCreateRequest:
    def test_minimal(self) -> None:
        req = AppointmentServiceItemCreateRequest(service_type=ServiceType.HOMEMAKING)
        assert req.planned_minutes is None
        assert req.notes is None

    def test_planned_minutes_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            AppointmentServiceItemCreateRequest(
                service_type=ServiceType.PERSONAL_CARE,
                planned_minutes=0,
            )
        with pytest.raises(ValidationError):
            AppointmentServiceItemCreateRequest(
                service_type=ServiceType.PERSONAL_CARE,
                planned_minutes=-5,
            )

    def test_planned_minutes_upper_bound(self) -> None:
        # 24h * 60min = 1440 should be allowed
        req = AppointmentServiceItemCreateRequest(
            service_type=ServiceType.PERSONAL_CARE,
            planned_minutes=1440,
        )
        assert req.planned_minutes == 1440
        with pytest.raises(ValidationError):
            AppointmentServiceItemCreateRequest(
                service_type=ServiceType.PERSONAL_CARE,
                planned_minutes=1441,
            )


# --------------------------------------------------------------------------
# AppointmentServiceItemUpdateRequest
# --------------------------------------------------------------------------
class TestAppointmentServiceItemUpdateRequest:
    def test_empty_update(self) -> None:
        req = AppointmentServiceItemUpdateRequest()
        assert req.service_type is None
        assert req.status is None

    def test_status_change(self) -> None:
        req = AppointmentServiceItemUpdateRequest(status=ServiceItemStatus.DONE)
        assert req.status == ServiceItemStatus.DONE

    def test_planned_minutes_must_be_positive_when_set(self) -> None:
        with pytest.raises(ValidationError):
            AppointmentServiceItemUpdateRequest(planned_minutes=0)
