"""Unit tests for the visits service state-machine helper.

Pure-Python — verifies the `_is_transition_allowed` function in
`visits.service` only. Complements the schema tests and protects the
visit lifecycle from accidental edits.

Per migration 0027 / spec alignment, the lifecycle is now:
    SCHEDULED → READY → IN_PROGRESS → AWAITING_SIGNATURE → COMPLETED
                ↘  CANCELLED / MISSED / REJECTED  ↙

The visit mirrors the appointment lifecycle 1:1.
"""

from __future__ import annotations

import pytest

from src.modules.visits.service import _is_transition_allowed
from src.shared.domain.enums import VisitStatus


# --------------------------------------------------------------------------
# Happy-path forward transitions
# --------------------------------------------------------------------------
class TestAllowedTransitions:
    @pytest.mark.parametrize(
        "from_state,to_state",
        [
            (VisitStatus.SCHEDULED, VisitStatus.READY),
            (VisitStatus.READY, VisitStatus.IN_PROGRESS),
            (VisitStatus.IN_PROGRESS, VisitStatus.AWAITING_SIGNATURE),
            (VisitStatus.AWAITING_SIGNATURE, VisitStatus.COMPLETED),
        ],
    )
    def test_happy_path_edges_allowed(
        self, from_state: VisitStatus, to_state: VisitStatus
    ) -> None:
        assert _is_transition_allowed(from_state, to_state) is True


# --------------------------------------------------------------------------
# Cancellation allowed from any pre-completion state
# --------------------------------------------------------------------------
class TestCancellation:
    @pytest.mark.parametrize(
        "from_state",
        [
            VisitStatus.SCHEDULED,
            VisitStatus.READY,
            VisitStatus.IN_PROGRESS,
            VisitStatus.AWAITING_SIGNATURE,
        ],
    )
    def test_cancellation_allowed_from_pre_completion(
        self, from_state: VisitStatus
    ) -> None:
        assert (
            _is_transition_allowed(from_state, VisitStatus.CANCELLED) is True
        )

    def test_cancellation_blocked_after_completed(self) -> None:
        assert (
            _is_transition_allowed(VisitStatus.COMPLETED, VisitStatus.CANCELLED)
            is False
        )


# --------------------------------------------------------------------------
# Terminal states have no outbound edges
# --------------------------------------------------------------------------
class TestTerminalStates:
    @pytest.mark.parametrize(
        "from_state",
        [
            VisitStatus.COMPLETED,
            VisitStatus.CANCELLED,
            VisitStatus.MISSED,
            VisitStatus.REJECTED,
        ],
    )
    def test_no_outbound_transitions(
        self, from_state: VisitStatus
    ) -> None:
        for to_state in VisitStatus:
            assert _is_transition_allowed(from_state, to_state) is False


# --------------------------------------------------------------------------
# Invalid jumps and backwards transitions are blocked
# --------------------------------------------------------------------------
class TestInvalidTransitions:
    @pytest.mark.parametrize(
        "from_state,to_state",
        [
            # Skipping states is forbidden
            (VisitStatus.SCHEDULED, VisitStatus.IN_PROGRESS),
            (VisitStatus.SCHEDULED, VisitStatus.COMPLETED),
            (VisitStatus.READY, VisitStatus.AWAITING_SIGNATURE),
            (VisitStatus.READY, VisitStatus.COMPLETED),
            (VisitStatus.IN_PROGRESS, VisitStatus.COMPLETED),
            # Going backward is forbidden
            (VisitStatus.READY, VisitStatus.SCHEDULED),
            (VisitStatus.IN_PROGRESS, VisitStatus.READY),
            (VisitStatus.AWAITING_SIGNATURE, VisitStatus.IN_PROGRESS),
            (VisitStatus.COMPLETED, VisitStatus.AWAITING_SIGNATURE),
        ],
    )
    def test_invalid_edges_blocked(
        self, from_state: VisitStatus, to_state: VisitStatus
    ) -> None:
        assert _is_transition_allowed(from_state, to_state) is False

    def test_self_transition_not_in_machine(self) -> None:
        # Self-transitions are handled by the service layer as a no-op;
        # the machine itself returns False.
        assert _is_transition_allowed(
            VisitStatus.READY, VisitStatus.READY
        ) is False
