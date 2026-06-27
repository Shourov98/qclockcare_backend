"""Unit tests for `src/modules/auth/email_service.py`.

These tests do NOT touch the network. They:

1. Verify the `EmailMessage` shape built by `_build_otp_email` and
   `_build_reset_email` — subject, From, To, body contains the OTP /
   reset token + the deep-link URL.
2. Verify `send_otp_email` / `send_password_reset_email` schedule a
   background task with the right arguments.
3. Verify the background runner logs the OTP when
   `LOG_INCLUDE_DEV_OTPS=true` and stays silent otherwise.
4. Verify a provider crash in the background runner is swallowed
   (logged at error level, never raised).

The real `EmailProvider` is mocked so we can capture the call
without touching SMTP.
"""

from __future__ import annotations

import uuid
from email.message import EmailMessage
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Email-message shape
# ---------------------------------------------------------------------------
class TestBuildOtpEmail:
    def _build(
        self,
        *,
        to_email: str = "user@example.com",
        to_name: str | None = "Alex",
        otp: str = "123456",
        expires_in_minutes: int = 10,
        frontend_url: str = "http://localhost:3000",
    ) -> EmailMessage:
        from src.modules.auth.email_service import _build_otp_email

        with patch("src.modules.auth.email_service.settings") as mock_settings:
            mock_settings.FRONTEND_URL = frontend_url
            mock_settings.SMTP_FROM_NAME = "QlockCare"
            mock_settings.SMTP_FROM_EMAIL = "noreply@qlockcare.local"
            return _build_otp_email(
                to_email=to_email,
                to_name=to_name,
                otp=otp,
                expires_in_minutes=expires_in_minutes,
            )

    def test_subject_contains_brand(self) -> None:
        msg = self._build()
        assert "QlockCare" in msg["Subject"]

    def test_from_header_uses_settings(self) -> None:
        msg = self._build()
        assert msg["From"] == "QlockCare <noreply@qlockcare.local>"

    def test_to_header_is_recipient(self) -> None:
        msg = self._build(to_email="alex@example.com")
        assert msg["To"] == "alex@example.com"

    def test_body_contains_otp(self) -> None:
        msg = self._build(otp="987654")
        body = _body(msg)
        assert "987654" in body

    def test_body_contains_deep_link(self) -> None:
        msg = self._build(
            otp="987654",
            to_email="alex@example.com",
            frontend_url="http://localhost:3000",
        )
        body = _body(msg)
        # URL must contain the email + OTP as query params so the SPA
        # can pre-fill them.
        assert "http://localhost:3000/verify-email" in body
        assert "otp=987654" in body
        assert "email=alex%40example.com" in body or "email=alex@example.com" in body

    def test_body_greets_user_by_name(self) -> None:
        msg = self._build(to_name="Sam")
        assert "Hi Sam" in _body(msg)

    def test_body_skips_name_when_none(self) -> None:
        msg = self._build(to_name=None)
        # Still a greeting, just no name.
        assert "Hi,\n" in _body(msg)

    def test_body_mentions_expiry(self) -> None:
        msg = self._build(expires_in_minutes=15)
        assert "15 minutes" in _body(msg)

    def test_strips_trailing_slash_from_frontend_url(self) -> None:
        msg = self._build(frontend_url="http://localhost:3000/")
        body = _body(msg)
        # No double slash.
        assert "localhost:3000//verify-email" not in body
        assert "localhost:3000/verify-email" in body


class TestBuildResetEmail:
    def _build(
        self,
        *,
        to_email: str = "user@example.com",
        to_name: str | None = "Alex",
        reset_token: str = "reset-token-abc",
        expires_in_minutes: int = 120,
        frontend_url: str = "http://localhost:3000",
    ) -> EmailMessage:
        from src.modules.auth.email_service import _build_reset_email

        with patch("src.modules.auth.email_service.settings") as mock_settings:
            mock_settings.FRONTEND_URL = frontend_url
            mock_settings.SMTP_FROM_NAME = "QlockCare"
            mock_settings.SMTP_FROM_EMAIL = "noreply@qlockcare.local"
            return _build_reset_email(
                to_email=to_email,
                to_name=to_name,
                reset_token=reset_token,
                expires_in_minutes=expires_in_minutes,
            )

    def test_subject_mentions_reset(self) -> None:
        msg = self._build()
        assert "password" in msg["Subject"].lower()

    def test_body_contains_reset_link(self) -> None:
        msg = self._build(
            reset_token="abcdef",
            frontend_url="http://localhost:3000",
        )
        body = _body(msg)
        assert "http://localhost:3000/reset-password" in body
        assert "token=abcdef" in body

    def test_body_contains_expiry(self) -> None:
        msg = self._build(expires_in_minutes=120)
        assert "120 minutes" in _body(msg)


# ---------------------------------------------------------------------------
# BackgroundTasks scheduling
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestSchedulerShape:
    async def test_send_otp_email_adds_background_task(self) -> None:
        from src.modules.auth.email_service import send_otp_email

        background_tasks = MagicMock()
        with patch("src.modules.auth.email_service.settings") as mock_settings:
            mock_settings.OTP_EXPIRY_MINUTES = 10
            mock_settings.FRONTEND_URL = "http://localhost:3000"
            mock_settings.SMTP_FROM_NAME = "QlockCare"
            mock_settings.SMTP_FROM_EMAIL = "noreply@qlockcare.local"
            mock_settings.LOG_INCLUDE_DEV_OTPS = False
            send_otp_email(
                background_tasks,
                to_email="alex@example.com",
                to_name="Alex",
                otp="123456",
                expires_in_minutes=10,
                recipient_user_id=uuid.uuid4(),
            )
        background_tasks.add_task.assert_called_once()
        # First positional arg is the background runner.
        runner = background_tasks.add_task.call_args.args[0]
        assert runner.__name__ == "_send_in_background"
        # kwargs include the OTP (dev-only log field) and `kind="otp"`.
        kwargs = background_tasks.add_task.call_args.kwargs
        assert kwargs["kind"] == "otp"
        assert kwargs["dev_otp_for_test_only"] == "123456"
        assert kwargs["recipient_user_id"] is not None
        # The message has the right subject.
        msg = kwargs["message"]
        assert "QlockCare" in msg["Subject"]

    async def test_send_reset_email_adds_background_task(self) -> None:
        from src.modules.auth.email_service import send_password_reset_email

        background_tasks = MagicMock()
        with patch("src.modules.auth.email_service.settings") as mock_settings:
            mock_settings.FRONTEND_URL = "http://localhost:3000"
            mock_settings.SMTP_FROM_NAME = "QlockCare"
            mock_settings.SMTP_FROM_EMAIL = "noreply@qlockcare.local"
            mock_settings.LOG_INCLUDE_DEV_OTPS = False
            send_password_reset_email(
                background_tasks,
                to_email="alex@example.com",
                to_name="Alex",
                reset_token="reset-xyz",
                expires_in_minutes=120,
                recipient_user_id=uuid.uuid4(),
            )
        kwargs = background_tasks.add_task.call_args.kwargs
        assert kwargs["kind"] == "reset"
        assert kwargs["dev_otp_for_test_only"] == "reset-xyz"


# ---------------------------------------------------------------------------
# Background runner behaviour
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestSendInBackground:
    async def test_calls_email_provider_with_message_fields(self) -> None:
        """The runner must invoke `EmailProvider.send` with the
        to/subject/body fields from the EmailMessage."""
        from src.modules.auth.email_service import _send_in_background

        msg = EmailMessage()
        msg["From"] = "QlockCare <noreply@qlockcare.local>"
        msg["To"] = "alex@example.com"
        msg["Subject"] = "Verify your QlockCare account"
        msg.set_content("Welcome. Code: 424242\nhttp://localhost:3000/verify-email")

        provider = MagicMock()
        provider.send = AsyncMock(return_value=_ok_result())

        with (
            patch("src.modules.auth.email_service.settings") as mock_settings,
            patch("src.modules.auth.email_service.session_scope") as mock_scope,
            patch(
                "src.modules.auth.email_service.set_session_context",
                AsyncMock(),
            ),
            patch(
                # `EmailProvider` is imported lazily inside
                # `_send_in_background`, so we patch it at the source
                # module — `src.modules.notifications.channels` — to
                # intercept the lookup.
                "src.modules.notifications.channels.EmailProvider",
                return_value=provider,
            ),
        ):
            mock_settings.LOG_INCLUDE_DEV_OTPS = False
            mock_settings.SMTP_FROM_NAME = "QlockCare"
            mock_settings.SMTP_FROM_EMAIL = "noreply@qlockcare.local"
            mock_settings.SMTP_RETRY_MAX_ATTEMPTS = 1
            mock_scope.return_value = _null_scope()

            await _send_in_background(
                recipient_user_id=uuid.uuid4(),
                message=msg,
                dev_otp_for_test_only="424242",
                kind="otp",
            )

        provider.send.assert_called_once()
        call = provider.send.call_args.kwargs
        assert call["to"] == "alex@example.com"
        assert call["subject"] == "Verify your QlockCare account"
        assert "Code: 424242" in call["body"]

    async def test_dev_log_when_enabled(self) -> None:
        """When `LOG_INCLUDE_DEV_OTPS=true`, the OTP must appear in the
        log under a clearly-labelled `dev_*` field."""
        from src.modules.auth.email_service import _send_in_background

        msg = _make_msg()
        provider = MagicMock()
        provider.send = AsyncMock(return_value=_ok_result())

        with (
            patch("src.modules.auth.email_service.settings") as mock_settings,
            patch("src.modules.auth.email_service.session_scope") as mock_scope,
            patch(
                "src.modules.auth.email_service.set_session_context",
                AsyncMock(),
            ),
            patch(
                "src.modules.notifications.channels.EmailProvider",
                return_value=provider,
            ),
            patch("src.modules.auth.email_service.logger") as mock_logger,
        ):
            mock_settings.LOG_INCLUDE_DEV_OTPS = True
            mock_settings.SMTP_FROM_NAME = "QlockCare"
            mock_settings.SMTP_FROM_EMAIL = "noreply@qlockcare.local"
            mock_settings.SMTP_RETRY_MAX_ATTEMPTS = 1
            mock_scope.return_value = _null_scope()

            await _send_in_background(
                recipient_user_id=uuid.uuid4(),
                message=msg,
                dev_otp_for_test_only="DEV-OTP-12345",
                kind="otp",
            )

        info_calls = mock_logger.info.call_args_list
        assert any(
            "dev_otp" in call.args[0] for call in info_calls
        ), "expected at least one dev_otp log call"
        # And the structured field carries the plaintext OTP.
        assert any(
            (call.kwargs.get("extra") or {}).get("dev_otp_for_test_only")
            == "DEV-OTP-12345"
            for call in info_calls
        )

    async def test_no_dev_log_when_disabled(self) -> None:
        """When `LOG_INCLUDE_DEV_OTPS=false` (default), the OTP must
        NOT appear in any log call — production-safe default."""
        from src.modules.auth.email_service import _send_in_background

        msg = _make_msg()
        provider = MagicMock()
        provider.send = AsyncMock(return_value=_ok_result())

        with (
            patch("src.modules.auth.email_service.settings") as mock_settings,
            patch("src.modules.auth.email_service.session_scope") as mock_scope,
            patch(
                "src.modules.auth.email_service.set_session_context",
                AsyncMock(),
            ),
            patch(
                "src.modules.notifications.channels.EmailProvider",
                return_value=provider,
            ),
            patch("src.modules.auth.email_service.logger") as mock_logger,
        ):
            mock_settings.LOG_INCLUDE_DEV_OTPS = False
            mock_settings.SMTP_FROM_NAME = "QlockCare"
            mock_settings.SMTP_FROM_EMAIL = "noreply@qlockcare.local"
            mock_settings.SMTP_RETRY_MAX_ATTEMPTS = 1
            mock_scope.return_value = _null_scope()

            await _send_in_background(
                recipient_user_id=uuid.uuid4(),
                message=msg,
                dev_otp_for_test_only="SECRET-OTP-67890",
                kind="otp",
            )

        for call in mock_logger.info.call_args_list:
            extra = call.kwargs.get("extra") or {}
            assert "SECRET-OTP-67890" not in str(extra)
            assert "SECRET-OTP-67890" not in str(call.args)

    async def test_provider_crash_is_swallowed(self) -> None:
        """If `provider.send` raises (e.g. SMTP server is down), the
        background runner must NOT raise — it logs at error level."""
        from src.modules.auth.email_service import _send_in_background

        msg = _make_msg()
        provider = MagicMock()
        provider.send = AsyncMock(side_effect=RuntimeError("smtp unreachable"))

        with (
            patch("src.modules.auth.email_service.settings") as mock_settings,
            patch("src.modules.auth.email_service.session_scope") as mock_scope,
            patch(
                "src.modules.auth.email_service.set_session_context",
                AsyncMock(),
            ),
            patch(
                "src.modules.notifications.channels.EmailProvider",
                return_value=provider,
            ),
            patch("src.modules.auth.email_service.logger") as mock_logger,
        ):
            mock_settings.LOG_INCLUDE_DEV_OTPS = False
            mock_settings.SMTP_FROM_NAME = "QlockCare"
            mock_settings.SMTP_FROM_EMAIL = "noreply@qlockcare.local"
            mock_settings.SMTP_RETRY_MAX_ATTEMPTS = 1
            mock_scope.return_value = _null_scope()

            # Must not raise.
            await _send_in_background(
                recipient_user_id=uuid.uuid4(),
                message=msg,
                dev_otp_for_test_only="x",
                kind="otp",
            )

        # Error was logged.
        assert mock_logger.error.called


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _body(msg: EmailMessage) -> str:
    """Read the plaintext body of an EmailMessage."""
    payload = msg.get_content()
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    return payload


def _make_msg() -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "QlockCare <noreply@qlockcare.local>"
    msg["To"] = "alex@example.com"
    msg["Subject"] = "Verify your QlockCare account"
    msg.set_content("body")
    return msg


def _ok_result() -> Any:
    """Provider success result shape."""

    class _R:
        success = True
        provider_message_id = None
        error = None

    return _R()


class _NullSessionCM:
    async def __aenter__(self) -> MagicMock:
        return MagicMock()

    async def __aexit__(self, *_: Any) -> None:
        return None


def _null_scope() -> _NullSessionCM:
    return _NullSessionCM()


__all__ = [
    "TestBuildOtpEmail",
    "TestBuildResetEmail",
    "TestSchedulerShape",
    "TestSendInBackground",
]
