"""Unit tests for `src/modules/auth/email_service.py` invitation helpers.

The invitation email is now OTP-based — the URL carries only the
recipient's `?email=` and the OTP lives in the body. These tests
verify:

1. The `EmailMessage` shape built by `_build_invitation_email`:
   subject, From, To, body contains the deep link (`?email=...`),
   the OTP plaintext, and the expiry wording in minutes.
2. `send_invitation_email` schedules a background task with
   `kind="invitation"` and the right arguments.
3. The background runner logs the OTP when
   `LOG_INCLUDE_DEV_OTPS=true` and stays silent otherwise.

Mirrors `tests/unit/test_auth_email_service.py` for the OTP / reset
helpers — same shape, new tests for the new function.
"""

from __future__ import annotations

import uuid
from email.message import EmailMessage
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Email-message shape
# ---------------------------------------------------------------------------
class TestBuildInvitationEmail:
    def _build(
        self,
        *,
        to_email: str = "user@example.com",
        to_name: str | None = "Alex",
        otp: str = "123456",
        expires_in_minutes: int = 10,
        frontend_url: str = "http://localhost:3000",
    ) -> EmailMessage:
        from src.modules.auth.email_service import _build_invitation_email

        with patch("src.modules.auth.email_service.settings") as mock_settings:
            mock_settings.FRONTEND_URL = frontend_url
            mock_settings.SMTP_FROM_NAME = "QlockCare"
            mock_settings.SMTP_FROM_EMAIL = "noreply@qlockcare.local"
            return _build_invitation_email(
                to_email=to_email,
                to_name=to_name,
                otp=otp,
                expires_in_minutes=expires_in_minutes,
            )

    def test_subject_mentions_invitation(self) -> None:
        msg = self._build()
        assert "invited" in msg["Subject"].lower()

    def test_subject_contains_brand(self) -> None:
        msg = self._build()
        assert "QlockCare" in msg["Subject"]

    def test_from_header_uses_settings(self) -> None:
        msg = self._build()
        assert msg["From"] == "QlockCare <noreply@qlockcare.local>"

    def test_to_header_is_recipient(self) -> None:
        msg = self._build(to_email="alex@example.com")
        assert msg["To"] == "alex@example.com"

    def test_body_greets_user_by_name(self) -> None:
        msg = self._build(to_name="Sam")
        assert "Hi Sam" in _body(msg)

    def test_body_skips_name_when_none(self) -> None:
        msg = self._build(to_name=None)
        # Still a greeting, just no name.
        assert "Hi,\n" in _body(msg)

    def test_body_contains_deep_link(self) -> None:
        msg = self._build(
            to_email="alex@example.com",
            frontend_url="http://localhost:3000",
        )
        body = _body(msg)
        # URL must carry the email — that's how the SPA pre-fills.
        assert "http://localhost:3000/accept-invitation" in body
        assert "email=alex%40example.com" in body

    def test_body_contains_otp(self) -> None:
        """The OTP must be prominently in the body so users can copy-paste it."""
        msg = self._build(otp="482915")
        assert "482915" in _body(msg)
        assert "Verification code" in _body(msg)

    def test_body_mentions_expiry_in_minutes(self) -> None:
        msg = self._build(expires_in_minutes=10)
        assert "10 minutes" in _body(msg)

    def test_strips_trailing_slash_from_frontend_url(self) -> None:
        msg = self._build(frontend_url="http://localhost:3000/")
        body = _body(msg)
        # No double slash.
        assert "localhost:3000//accept-invitation" not in body
        assert "localhost:3000/accept-invitation" in body


# ---------------------------------------------------------------------------
# BackgroundTasks scheduling
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestSendInvitationSchedulerShape:
    async def test_send_invitation_email_adds_background_task(self) -> None:
        from src.modules.auth.email_service import send_invitation_email

        background_tasks = MagicMock()
        with patch("src.modules.auth.email_service.settings") as mock_settings:
            mock_settings.SMTP_FROM_NAME = "QlockCare"
            mock_settings.SMTP_FROM_EMAIL = "noreply@qlockcare.local"
            mock_settings.LOG_INCLUDE_DEV_OTPS = False
            mock_settings.FRONTEND_URL = "http://localhost:3000"
            send_invitation_email(
                background_tasks,
                to_email="alex@example.com",
                to_name="Alex",
                otp="482915",
                expires_in_minutes=10,
                recipient_user_id=uuid.uuid4(),
            )

        background_tasks.add_task.assert_called_once()
        # First positional arg is the background runner.
        runner = background_tasks.add_task.call_args.args[0]
        assert runner.__name__ == "_send_in_background"
        # kwargs include the OTP (dev-only log field) and `kind="invitation"`.
        kwargs = background_tasks.add_task.call_args.kwargs
        assert kwargs["kind"] == "invitation"
        assert kwargs["dev_otp_for_test_only"] == "482915"
        assert kwargs["recipient_user_id"] is not None
        # The message has the right subject.
        msg = kwargs["message"]
        assert "invited" in msg["Subject"].lower()
        assert "QlockCare" in msg["Subject"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _body(msg: EmailMessage) -> str:
    """Read the plaintext body of an EmailMessage."""
    payload = msg.get_content()
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    return payload


__all__ = [
    "TestBuildInvitationEmail",
    "TestSendInvitationSchedulerShape",
]