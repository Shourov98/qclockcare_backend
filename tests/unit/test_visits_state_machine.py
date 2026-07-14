"""Unit tests for the visits service state-machine helper.

Pure-Python — verifies the `_is_transition_allowed` function in
`visits.service` only. Complements the schema tests and protects the
visit lifecycle from accidental edits.
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
            (VisitStatus.CHECKED_IN, VisitStatus.IN_PROGRESS),
            (VisitStatus.IN_PROGRESS, VisitStatus.CHECKED_OUT),
            (VisitStatus.CHECKED_OUT, VisitStatus.COMPLETED),
        ],
    )
    def test_happy_path_edges_allowed(
        self, from_state: VisitStatus, to_state: VisitStatus
    ) -> None:
        assert _is_transition_allowed(from_state, to_state) is True


# --------------------------------------------------------------------------
# COMPLETED is terminal
# --------------------------------------------------------------------------
class TestTerminalState:
    @pytest.mark.parametrize(
        "to_state",
        [
            VisitStatus.CHECKED_IN,
            VisitStatus.IN_PROGRESS,
            VisitStatus.CHECKED_OUT,
        ],
    )
    def test_completed_is_terminal(self, to_state: VisitStatus) -> None:
        assert _is_transition_allowed(VisitStatus.COMPLETED, to_state) is False


# --------------------------------------------------------------------------
# Invalid jumps
# --------------------------------------------------------------------------
class TestInvalidJumps:
    def test_cannot_skip_in_progress(self) -> None:
        # CHECKED_IN → CHECKED_OUT skips IN_PROGRESS
        assert _is_transition_allowed(
            VisitStatus.CHECKED_IN, VisitStatus.CHECKED_OUT
        ) is False

    def test_cannot_skip_checked_out(self) -> None:
        # IN_PROGRESS → COMPLETED skips CHECKED_OUT
        assert _is_transition_allowed(
            VisitStatus.IN_PROGRESS, VisitStatus.COMPLETED
        ) is False

    def test_cannot_jump_to_completed_from_checked_in(self) -> None:
        assert _is_transition_allowed(
            VisitStatus.CHECKED_IN, VisitStatus.COMPLETED
        ) is False

    def test_cannot_go_backwards(self) -> None:
        # CHECKED_OUT → IN_PROGRESS
        assert _is_transition_allowed(
            VisitStatus.CHECKED_OUT, VisitStatus.IN_PROGRESS
        ) is False
        # IN_PROGRESS → CHECKED_IN
        assert _is_transition_allowed(
            VisitStatus.IN_PROGRESS, VisitStatus.CHECKED_IN
        ) is False

    def test_self_transition_not_in_machine(self) -> None:
        # Self-transitions are handled by the service layer as a no-op;
        # the machine itself returns False.
        assert _is_transition_allowed(
            VisitStatus.CHECKED_IN, VisitStatus.CHECKED_IN
        ) is False
