"""Unit tests for the appointments service state-machine helper.

Pure-Python — verifies the `_is_transition_allowed` function in
`appointments.service` only. This complements the schema tests and
protects the lifecycle from accidental edits.

Per migration 0027 / spec alignment, the lifecycle is now:
    SCHEDULED → READY → IN_PROGRESS → AWAITING_SIGNATURE → COMPLETED
                ↘  CANCELLED / MISSED / REJECTED  ↙
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
            (AppointmentStatus.SCHEDULED, AppointmentStatus.READY),
            (AppointmentStatus.READY, AppointmentStatus.IN_PROGRESS),
            (AppointmentStatus.IN_PROGRESS, AppointmentStatus.AWAITING_SIGNATURE),
            (AppointmentStatus.AWAITING_SIGNATURE, AppointmentStatus.COMPLETED),
        ],
    )
    def test_happy_path_edges_allowed(
        self, from_state: AppointmentStatus, to_state: AppointmentStatus
    ) -> None:
        assert _is_transition_allowed(from_state, to_state) is True


# --------------------------------------------------------------------------
# Cancellation can happen from anywhere pre-completion
# --------------------------------------------------------------------------
class TestCancellation:
    @pytest.mark.parametrize(
        "from_state",
        [
            AppointmentStatus.SCHEDULED,
            AppointmentStatus.READY,
            AppointmentStatus.IN_PROGRESS,
            AppointmentStatus.AWAITING_SIGNATURE,
        ],
    )
    def test_cancellation_allowed_from_pre_completion(
        self, from_state: AppointmentStatus
    ) -> None:
        assert (
            _is_transition_allowed(from_state, AppointmentStatus.CANCELLED) is True
        )

    def test_cancellation_blocked_after_completed(self) -> None:
        assert (
            _is_transition_allowed(AppointmentStatus.COMPLETED, AppointmentStatus.CANCELLED)
            is False
        )


# --------------------------------------------------------------------------
# MISSED + REJECTED allow transitions from early states only
# --------------------------------------------------------------------------
class TestMissedAndRejected:
    @pytest.mark.parametrize(
        "from_state,to_state",
        [
            (AppointmentStatus.SCHEDULED, AppointmentStatus.MISSED),
            (AppointmentStatus.SCHEDULED, AppointmentStatus.REJECTED),
            (AppointmentStatus.READY, AppointmentStatus.MISSED),
            (AppointmentStatus.IN_PROGRESS, AppointmentStatus.MISSED),
        ],
    )
    def test_missed_and_rejected_from_early_states(
        self, from_state: AppointmentStatus, to_state: AppointmentStatus
    ) -> None:
        assert _is_transition_allowed(from_state, to_state) is True

    def test_missed_blocked_after_completed(self) -> None:
        assert (
            _is_transition_allowed(AppointmentStatus.COMPLETED, AppointmentStatus.MISSED)
            is False
        )

    def test_rejected_blocked_after_completed(self) -> None:
        assert (
            _is_transition_allowed(AppointmentStatus.COMPLETED, AppointmentStatus.REJECTED)
            is False
        )


# --------------------------------------------------------------------------
# Terminal states have no outbound edges
# --------------------------------------------------------------------------
class TestTerminalStates:
    @pytest.mark.parametrize(
        "from_state",
        [
            AppointmentStatus.COMPLETED,
            AppointmentStatus.CANCELLED,
            AppointmentStatus.MISSED,
            AppointmentStatus.REJECTED,
        ],
    )
    def test_no_outbound_transitions(
        self, from_state: AppointmentStatus
    ) -> None:
        for to_state in AppointmentStatus:
            assert _is_transition_allowed(from_state, to_state) is False


# --------------------------------------------------------------------------
# Invalid forward transitions are rejected
# --------------------------------------------------------------------------
class TestInvalidTransitions:
    @pytest.mark.parametrize(
        "from_state,to_state",
        [
            # Skipping states is forbidden
            (AppointmentStatus.SCHEDULED, AppointmentStatus.IN_PROGRESS),
            (AppointmentStatus.SCHEDULED, AppointmentStatus.COMPLETED),
            (AppointmentStatus.READY, AppointmentStatus.AWAITING_SIGNATURE),
            (AppointmentStatus.READY, AppointmentStatus.COMPLETED),
            (AppointmentStatus.IN_PROGRESS, AppointmentStatus.COMPLETED),
            # Going backward is forbidden
            (AppointmentStatus.READY, AppointmentStatus.SCHEDULED),
            (AppointmentStatus.IN_PROGRESS, AppointmentStatus.READY),
            (AppointmentStatus.AWAITING_SIGNATURE, AppointmentStatus.IN_PROGRESS),
            (AppointmentStatus.COMPLETED, AppointmentStatus.AWAITING_SIGNATURE),
        ],
    )
    def test_invalid_edges_blocked(
        self, from_state: AppointmentStatus, to_state: AppointmentStatus
    ) -> None:
        assert _is_transition_allowed(from_state, to_state) is False

    def test_self_transition_is_not_listed_in_machine(self) -> None:
        # Self-transitions are handled by the service layer as a no-op
        # before the machine is consulted; the machine itself returns False.
        assert _is_transition_allowed(
            AppointmentStatus.SCHEDULED, AppointmentStatus.SCHEDULED
        ) is False
