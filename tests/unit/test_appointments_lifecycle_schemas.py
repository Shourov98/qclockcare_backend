"""Unit tests for appointment lifecycle request/response schemas.

Covers the 5 new DTOs added by feat/appointment-lifecycle:
  - AppointmentConfirmRequest
  - AppointmentRescheduleRequest
  - AppointmentCancellationRequest
  - AppointmentConfirmationResponse
  - AppointmentEventResponse
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from src.modules.appointments.schemas import (
    AppointmentCancellationRequest,
    AppointmentConfirmationResponse,
    AppointmentConfirmRequest,
    AppointmentEventResponse,
    AppointmentRescheduleRequest,
)
from src.shared.domain.enums import (
    AppointmentEventType,
    AppointmentStatus,
    ConfirmationStatus,
    UserRole,
)


class TestAppointmentConfirmRequest:
    def test_defaults_to_confirmed(self) -> None:
        r = AppointmentConfirmRequest()
        assert r.declined is False
        assert r.comment is None

    def test_declined_flag(self) -> None:
        r = AppointmentConfirmRequest(declined=True, comment="Can't make it")
        assert r.declined is True
        assert r.comment == "Can't make it"

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AppointmentConfirmRequest.model_validate({"declined": False, "junk": True})


class TestAppointmentRescheduleRequest:
    def test_valid_window(self) -> None:
        r = AppointmentRescheduleRequest(
            proposed_start=datetime(2026, 7, 1, 14, 0, tzinfo=UTC),
            proposed_end=datetime(2026, 7, 1, 15, 0, tzinfo=UTC),
        )
        assert r.proposed_start < r.proposed_end

    def test_end_before_start_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AppointmentRescheduleRequest(
                proposed_start=datetime(2026, 7, 1, 15, 0, tzinfo=UTC),
                proposed_end=datetime(2026, 7, 1, 14, 0, tzinfo=UTC),
            )

    def test_equal_times_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AppointmentRescheduleRequest(
                proposed_start=datetime(2026, 7, 1, 14, 0, tzinfo=UTC),
                proposed_end=datetime(2026, 7, 1, 14, 0, tzinfo=UTC),
            )


class TestAppointmentCancellationRequest:
    def test_minimum(self) -> None:
        r = AppointmentCancellationRequest(reason="Patient unwell")
        assert r.reason == "Patient unwell"

    def test_empty_reason_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AppointmentCancellationRequest(reason="")

    def test_max_length_enforced(self) -> None:
        # 4001 chars is over the max_length=4000 cap.
        with pytest.raises(ValidationError):
            AppointmentCancellationRequest(reason="a" * 4001)


class TestAppointmentConfirmationResponse:
    def test_from_dict(self) -> None:
        # Use a dict (not an ORM instance) so unit tests don't trigger
        # SQLAlchemy mapper configuration for unrelated models.
        r = AppointmentConfirmationResponse.model_validate(
            {
                "id": uuid.uuid4(),
                "appointment_id": uuid.uuid4(),
                "confirmed_by": uuid.uuid4(),
                "confirmation_role": UserRole.PATIENT,
                "status": ConfirmationStatus.CONFIRMED,
                "comment": "ok",
                "created_at": datetime.now(UTC),
            }
        )
        assert r.confirmation_role == UserRole.PATIENT
        assert r.status == ConfirmationStatus.CONFIRMED
        assert r.comment == "ok"


class TestAppointmentEventResponse:
    def test_from_dict(self) -> None:
        appt_id = uuid.uuid4()
        agency_id = uuid.uuid4()
        r = AppointmentEventResponse.model_validate(
            {
                "id": uuid.uuid4(),
                "appointment_id": appt_id,
                "agency_id": agency_id,
                "actor_user_id": uuid.uuid4(),
                "event_type": AppointmentEventType.STATUS_TRANSITION,
                "from_status": AppointmentStatus.SCHEDULED,
                "to_status": AppointmentStatus.CONFIRMED,
                "metadata": {"reason": "patient confirmed"},
                "ip_address": None,
                "user_agent": None,
                "created_at": datetime.now(UTC),
            }
        )
        assert r.event_type == AppointmentEventType.STATUS_TRANSITION
        assert r.from_status == AppointmentStatus.SCHEDULED
        assert r.to_status == AppointmentStatus.CONFIRMED
        assert r.metadata_ == {"reason": "patient confirmed"}

    def test_minimal_event(self) -> None:
        r = AppointmentEventResponse.model_validate(
            {
                "id": uuid.uuid4(),
                "appointment_id": uuid.uuid4(),
                "agency_id": uuid.uuid4(),
                "actor_user_id": None,
                "event_type": AppointmentEventType.CONFIRMATION_FILED,
                "from_status": None,
                "to_status": None,
                "metadata": {},
                "ip_address": None,
                "user_agent": None,
                "created_at": datetime.now(UTC),
            }
        )
        assert r.actor_user_id is None
        assert r.from_status is None
        assert r.to_status is None
