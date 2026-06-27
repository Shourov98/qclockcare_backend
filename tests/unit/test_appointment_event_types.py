"""Unit tests for `AppointmentEventType` enum.

The enum drives the `appointment_events.event_type` column and the
service-layer event-appender. We pin the members here so any rename
of an event value is caught at the unit-test layer (the DB column is
text so a renamed enum value would silently still insert — but the
event-appender would raise ValueError if it can't find the member).
"""

from __future__ import annotations

import pytest

from src.shared.domain.enums import AppointmentEventType


class TestAppointmentEventType:
    def test_members(self) -> None:
        members = {m.value for m in AppointmentEventType}
        # All five documented event types must exist.
        assert members == {
            "STATUS_TRANSITION",
            "CONFIRMATION_FILED",
            "RESCHEDULE_REQUESTED",
            "CANCELLATION_REQUESTED",
            "CANCELLED_BY_ADMIN",
        }

    def test_string_round_trip(self) -> None:
        # StrEnum: member == string value (Postgres stores .value)
        assert AppointmentEventType.CONFIRMATION_FILED == "CONFIRMATION_FILED"
        assert AppointmentEventType("CANCELLED_BY_ADMIN") is (
            AppointmentEventType.CANCELLED_BY_ADMIN
        )

    def test_unknown_value_raises(self) -> None:
        with pytest.raises(ValueError):
            AppointmentEventType("NONSENSE_EVENT")

    def test_member_count(self) -> None:
        # Adding new members requires updating this — intentional.
        assert len(list(AppointmentEventType)) == 5
