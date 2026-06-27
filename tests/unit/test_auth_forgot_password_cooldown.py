"""Unit tests for `src/modules/identity/auth_service.forgot_password` cooldown.

The cooldown piggybacks on `auth_audit_events` — it queries the most
recent `PASSWORD_RESET_REQUESTED` event for the user and raises
`OtpResendCooldownError` if it's within `OTP_RESEND_COOLDOWN_SECONDS`.

This test mocks `select(...).where(...).order_by(...).limit(1)` so we
don't need a real DB. The session.execute side_effect returns a fake
scalar result.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.exceptions import OtpResendCooldownError
from src.shared.domain.enums import AuthAuditEventType

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakeScalarResult:
    """Minimal stand-in for `Result.scalar_one_or_none()`."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _FakeSession:
    """Captures `execute(...)` calls and returns canned scalar results."""

    def __init__(self, *, scalars: list[Any]) -> None:
        self._scalars = list(scalars)
        self._idx = 0
        self.execute = AsyncMock(side_effect=self._execute)
        self.add = MagicMock()

    async def _execute(self, stmt: Any) -> _FakeScalarResult:
        if self._idx >= len(self._scalars):
            raise AssertionError(
                f"_FakeSession.execute called too many times "
                f"({self._idx + 1} > {len(self._scalars)})"
            )
        value = self._scalars[self._idx]
        self._idx += 1
        return _FakeScalarResult(value)


def _user_row(user_id: uuid.UUID, email: str) -> SimpleNamespace:
    return SimpleNamespace(id=user_id, email=email, full_name="Alex")


def _recent_audit_event(seconds_ago: int) -> SimpleNamespace:
    return SimpleNamespace(
        created_at=datetime.now(tz=UTC) - timedelta(seconds=seconds_ago),
        event_type=AuthAuditEventType.PASSWORD_RESET_REQUESTED,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestForgotPasswordCooldown:
    async def test_raises_when_last_request_was_within_cooldown(self) -> None:
        from src.modules.identity import auth_service

        user_id = uuid.uuid4()
        email = "alex@example.com"
        # Last request was 10s ago — cooldown is 60s → should raise.
        session = _FakeSession(
            scalars=[_user_row(user_id, email), _recent_audit_event(10)]
        )

        with (
            patch.object(auth_service, "settings") as mock_settings,
            patch.object(auth_service, "jwt_service") as mock_jwt,
            patch.object(auth_service, "_record_audit", AsyncMock()),
        ):
            mock_settings.OTP_RESEND_COOLDOWN_SECONDS = 60

            with pytest.raises(OtpResendCooldownError) as exc_info:
                await auth_service.forgot_password(
                    session,
                    email=email,
                )

            assert exc_info.value.details["cooldown_seconds_remaining"] > 0
            # No token should be issued when we raise.
            mock_jwt.issue_single_use_token.assert_not_called()

    async def test_proceeds_when_last_request_was_outside_cooldown(self) -> None:
        """If the last reset request was longer ago than the cooldown,
        we issue a fresh token."""
        from src.modules.identity import auth_service

        user_id = uuid.uuid4()
        email = "alex@example.com"
        session = _FakeSession(
            scalars=[_user_row(user_id, email), _recent_audit_event(120)]
        )

        with (
            patch.object(auth_service, "settings") as mock_settings,
            patch.object(auth_service, "jwt_service") as mock_jwt,
            patch.object(auth_service, "_record_audit", AsyncMock()),
            # Mock the SingleUseToken constructor so we don't trigger
            # the full SQLAlchemy mapper init in a unit test.
            patch.object(auth_service, "SingleUseToken", MagicMock()),
        ):
            mock_settings.OTP_RESEND_COOLDOWN_SECONDS = 60
            mock_jwt.issue_single_use_token.return_value = ("plaintext-token", "jti-1")

            result = await auth_service.forgot_password(session, email=email)

        assert result == (user_id, email, "plaintext-token")
        mock_jwt.issue_single_use_token.assert_called_once()
        session.add.assert_called_once()  # SingleUseToken row added

    async def test_proceeds_when_no_prior_audit_event(self) -> None:
        """First-ever forgot-password request — no audit row to check,
        so we always proceed."""
        from src.modules.identity import auth_service

        user_id = uuid.uuid4()
        email = "alex@example.com"
        session = _FakeSession(
            scalars=[_user_row(user_id, email), None]
        )

        with (
            patch.object(auth_service, "settings") as mock_settings,
            patch.object(auth_service, "jwt_service") as mock_jwt,
            patch.object(auth_service, "_record_audit", AsyncMock()),
            patch.object(auth_service, "SingleUseToken", MagicMock()),
        ):
            mock_settings.OTP_RESEND_COOLDOWN_SECONDS = 60
            mock_jwt.issue_single_use_token.return_value = ("plaintext-token", "jti-1")

            result = await auth_service.forgot_password(session, email=email)

        assert result == (user_id, email, "plaintext-token")

    async def test_returns_none_for_unknown_email(self) -> None:
        """Unknown email → no user → return (None, None, None). Does
        NOT raise — keeps the no-account-existence-leak invariant."""
        from src.modules.identity import auth_service

        session = _FakeSession(scalars=[None])  # no user

        with patch.object(auth_service, "settings") as mock_settings:
            mock_settings.OTP_RESEND_COOLDOWN_SECONDS = 60
            result = await auth_service.forgot_password(
                session, email="ghost@example.com"
            )

        assert result == (None, None, None)


__all__ = ["TestForgotPasswordCooldown"]


# ---------------------------------------------------------------------------
# also exercise resend_otp return-type change
# ---------------------------------------------------------------------------
class TestResendOtpReturnType:
    async def test_returns_cooldown_int_and_otp(self) -> None:
        """`resend_otp` now returns `(cooldown_int, OtpIssueResult | None)`
        so the router can email the OTP without re-issuing it."""
        from src.modules.identity import auth_service
        from src.modules.identity.otp_service import OtpIssueResult

        user_id = uuid.uuid4()
        user_row = _user_row(user_id, "alex@example.com")
        issued = OtpIssueResult(
            user_id=user_id,
            email="alex@example.com",
            full_name="Alex",
            otp="999111",
            expires_at=datetime.now(tz=UTC) + timedelta(minutes=10),
        )

        session = _FakeSession(scalars=[user_row])

        with (
            patch.object(auth_service, "otp_service") as mock_otp,
            patch.object(auth_service, "_record_audit", AsyncMock()),
        ):
            mock_otp.resend_otp = AsyncMock(return_value=issued)

            cooldown, returned = await auth_service.resend_otp(
                session, email="alex@example.com"
            )

        assert cooldown == 0
        assert returned is issued

    async def test_returns_none_for_unknown_email(self) -> None:
        from src.modules.identity import auth_service

        session = _FakeSession(scalars=[None])

        cooldown, returned = await auth_service.resend_otp(
            session, email="ghost@example.com"
        )

        assert cooldown == 0
        assert returned is None


__all__ += ["TestResendOtpReturnType"]
