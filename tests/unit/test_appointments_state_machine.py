"""Unit tests for the appointments service state-machine helper.

Pure-Python — verifies the `_is_transition_allowed` function in
`appointments.service` only. This complements the schema tests and
protects the lifecycle from accidental edits.
"""

from __future__ import annotations

import pytest

from src.modules.appointments.service import _is_transition_allowed
from src.shared.domain.enums import AppointmentStatus


# --------------------------------------------------------------------------
# Happy-path forward transitions
# --------------------------------------------------------------------------
class TestAllowedTransitions:
    @pytest.mark.parametrize(
        "from_state,to_state",
        [
            (AppointmentStatus.DRAFT, AppointmentStatus.SCHEDULED),
            (AppointmentStatus.SCHEDULED, AppointmentStatus.AWAITING_CONFIRMATION),
            (
                AppointmentStatus.AWAITING_CONFIRMATION,
                AppointmentStatus.CONFIRMED,
            ),
            (AppointmentStatus.CONFIRMED, AppointmentStatus.ASSIGNED),
            (AppointmentStatus.ASSIGNED, AppointmentStatus.CHECKED_IN),
            (AppointmentStatus.CHECKED_IN, AppointmentStatus.IN_PROGRESS),
            (AppointmentStatus.IN_PROGRESS, AppointmentStatus.CHECKED_OUT),
            (AppointmentStatus.CHECKED_OUT, AppointmentStatus.COMPLETED),
            (
                AppointmentStatus.COMPLETED,
                AppointmentStatus.AWAITING_SERVICE_VERIFICATION,
            ),
            (
                AppointmentStatus.SERVICE_VERIFIED,
                AppointmentStatus.APPROVED_FOR_BILLING,
            ),
            (AppointmentStatus.APPROVED_FOR_BILLING, AppointmentStatus.PAID),
        ],
    )
    def test_happy_path_edges_allowed(
        self, from_state: AppointmentStatus, to_state: AppointmentStatus
    ) -> None:
        assert _is_transition_allowed(from_state, to_state) is True


# --------------------------------------------------------------------------
# Terminal states have no outbound edges
# --------------------------------------------------------------------------
class TestTerminalStates:
    @pytest.mark.parametrize(
        "to_state",
        [
            AppointmentStatus.SCHEDULED,
            AppointmentStatus.CONFIRMED,
            AppointmentStatus.ASSIGNED,
            AppointmentStatus.CHECKED_IN,
            AppointmentStatus.COMPLETED,
            AppointmentStatus.PAID,
        ],
    )
    def test_cancelled_is_terminal(self, to_state: AppointmentStatus) -> None:
        assert _is_transition_allowed(AppointmentStatus.CANCELLED, to_state) is False

    @pytest.mark.parametrize(
        "to_state",
        [
            AppointmentStatus.CHECKED_IN,
            AppointmentStatus.ASSIGNED,
            AppointmentStatus.SCHEDULED,
        ],
    )
    def test_paid_is_terminal(self, to_state: AppointmentStatus) -> None:
        assert _is_transition_allowed(AppointmentStatus.PAID, to_state) is False

    @pytest.mark.parametrize(
        "to_state",
        [
            AppointmentStatus.CHECKED_IN,
            AppointmentStatus.SCHEDULED,
        ],
    )
    def test_no_show_is_terminal(self, to_state: AppointmentStatus) -> None:
        assert _is_transition_allowed(AppointmentStatus.NO_SHOW, to_state) is False

    @pytest.mark.parametrize(
        "to_state",
        [
            AppointmentStatus.SCHEDULED,
            AppointmentStatus.CONFIRMED,
        ],
    )
    def test_rejected_is_terminal(self, to_state: AppointmentStatus) -> None:
        assert _is_transition_allowed(AppointmentStatus.REJECTED, to_state) is False


# --------------------------------------------------------------------------
# Specific business-rule edges
# --------------------------------------------------------------------------
class TestBusinessRules:
    def test_cancellation_can_happen_from_draft(self) -> None:
        assert (
            _is_transition_allowed(AppointmentStatus.DRAFT, AppointmentStatus.CANCELLED)
            is True
        )

    def test_cancellation_can_happen_from_scheduled(self) -> None:
        assert (
            _is_transition_allowed(
                AppointmentStatus.SCHEDULED, AppointmentStatus.CANCELLED
            )
            is True
        )

    def test_cancellation_blocked_after_checked_in(self) -> None:
        assert (
            _is_transition_allowed(
                AppointmentStatus.CHECKED_IN, AppointmentStatus.CANCELLED
            )
            is False
        )

    def test_cancellation_blocked_after_completed(self) -> None:
        assert (
            _is_transition_allowed(
                AppointmentStatus.COMPLETED, AppointmentStatus.CANCELLED
            )
            is False
        )

    def test_reschedule_can_loop_back_to_scheduled(self) -> None:
        assert (
            _is_transition_allowed(
                AppointmentStatus.RESCHEDULE_REQUESTED,
                AppointmentStatus.SCHEDULED,
            )
            is True
        )

    def test_self_transition_is_not_listed_in_machine(self) -> None:
        # Self-transitions are handled by the service layer as a no-op
        # before the machine is consulted; the machine itself returns False.
        assert _is_transition_allowed(
            AppointmentStatus.SCHEDULED, AppointmentStatus.SCHEDULED
        ) is False
