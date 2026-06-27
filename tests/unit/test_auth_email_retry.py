"""Unit tests for `auth.email_service._send_in_background` retry
loop + `_compute_backoff_delay` helper.

The retry layer is critical for production reliability: a flaky SMTP
server during signup must not silently drop the invitation / OTP /
reset email. These tests cover:

1. `_compute_backoff_delay` — pure function, exponential + jitter +
   capped at max.
2. `_send_in_background` retries on `DeliveryResult(success=False)`
   and recovers on the second attempt.
3. `_send_in_background` gives up after `SMTP_RETRY_MAX_ATTEMPTS`
   failures and logs `send_exhausted` at error level.
4. `_send_in_background` does NOT retry when the first attempt
   succeeds.
5. `_send_in_background` treats an unexpected exception from
   `provider.send` as one failed attempt and keeps going (backstop).
6. `_send_in_background` logs `send_failed` with `attempt`,
   `max_attempts`, and `delay_seconds` before each sleep.
7. `SMTP_RETRY_MAX_ATTEMPTS=1` disables retries entirely.

All tests patch `asyncio.sleep` at the source location so retries
are instantaneous, matching the existing pattern of patching at
the source module in `test_auth_email_service.py`.
"""

from __future__ import annotations

from email.message import EmailMessage
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakeResult:
    """Mimics `DeliveryResult` — the dataclass returned by
    `EmailProvider.send`. `success` controls whether the retry
    loop considers the attempt successful."""

    def __init__(self, success: bool, error: str | None = None) -> None:
        self.success = success
        self.provider_message_id = None
        self.error = error


class _NullSessionCM:
    async def __aenter__(self) -> MagicMock:
        return MagicMock()

    async def __aexit__(self, *_: Any) -> None:
        return None


def _null_scope() -> _NullSessionCM:
    return _NullSessionCM()


def _make_msg(to: str = "alex@example.com") -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "QlockCare <noreply@qlockcare.local>"
    msg["To"] = to
    msg["Subject"] = "Verify your QlockCare account"
    msg.set_content("body")
    return msg


def _make_settings(
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    jitter: float = 0.5,
) -> Any:
    """Build a settings stand-in with retry knobs pre-configured."""
    s = SimpleNamespace()
    s.LOG_INCLUDE_DEV_OTPS = False
    s.SMTP_FROM_NAME = "QlockCare"
    s.SMTP_FROM_EMAIL = "noreply@qlockcare.local"
    s.SMTP_RETRY_MAX_ATTEMPTS = max_attempts
    s.SMTP_RETRY_BASE_DELAY_SECONDS = base_delay
    s.SMTP_RETRY_MAX_DELAY_SECONDS = max_delay
    s.SMTP_RETRY_JITTER = jitter
    return s


# ---------------------------------------------------------------------------
# _compute_backoff_delay — pure function
# ---------------------------------------------------------------------------
class TestComputeBackoffDelay:
    def test_first_retry_uses_base_delay_with_jitter(self) -> None:
        """`attempt=1` should produce a value in
        [base * (1 - jitter), base * (1 + jitter)]."""
        from src.modules.auth import email_service

        with patch.object(email_service, "settings") as mock_settings:
            mock_settings.SMTP_RETRY_BASE_DELAY_SECONDS = 1.0
            mock_settings.SMTP_RETRY_MAX_DELAY_SECONDS = 100.0
            mock_settings.SMTP_RETRY_JITTER = 0.5
            # attempt=1 → 1.0 * 2^0 = 1.0, with ±50% jitter.
            for _ in range(20):
                d = email_service._compute_backoff_delay(attempt=1)
                assert 0.5 <= d <= 1.5

    def test_exponential_growth_per_attempt(self) -> None:
        """`attempt=2` -> 2*base, `attempt=3` -> 4*base (capped at max)."""
        from src.modules.auth import email_service

        with patch.object(email_service, "settings") as mock_settings:
            mock_settings.SMTP_RETRY_BASE_DELAY_SECONDS = 1.0
            mock_settings.SMTP_RETRY_MAX_DELAY_SECONDS = 100.0
            mock_settings.SMTP_RETRY_JITTER = 0.0  # disable jitter for math

            assert email_service._compute_backoff_delay(attempt=1) == 1.0
            assert email_service._compute_backoff_delay(attempt=2) == 2.0
            assert email_service._compute_backoff_delay(attempt=3) == 4.0
            assert email_service._compute_backoff_delay(attempt=4) == 8.0

    def test_capped_at_max_delay(self) -> None:
        """`min(base * 2**(attempt-1), max_delay)` — at high attempt
        counts, the delay is capped."""
        from src.modules.auth import email_service

        with patch.object(email_service, "settings") as mock_settings:
            mock_settings.SMTP_RETRY_BASE_DELAY_SECONDS = 1.0
            mock_settings.SMTP_RETRY_MAX_DELAY_SECONDS = 5.0
            mock_settings.SMTP_RETRY_JITTER = 0.0

            # attempt=1 → 1.0, attempt=2 → 2.0, attempt=3 → 4.0,
            # attempt=4 → capped at 5.0, attempt=10 → capped at 5.0.
            assert email_service._compute_backoff_delay(attempt=4) == 5.0
            assert email_service._compute_backoff_delay(attempt=10) == 5.0

    def test_zero_jitter_returns_exact_value(self) -> None:
        """`SMTP_RETRY_JITTER=0` should give deterministic delays
        (useful for deterministic ops behaviour)."""
        from src.modules.auth import email_service

        with patch.object(email_service, "settings") as mock_settings:
            mock_settings.SMTP_RETRY_BASE_DELAY_SECONDS = 2.0
            mock_settings.SMTP_RETRY_MAX_DELAY_SECONDS = 100.0
            mock_settings.SMTP_RETRY_JITTER = 0.0

            assert email_service._compute_backoff_delay(attempt=1) == 2.0
            assert email_service._compute_backoff_delay(attempt=2) == 4.0


# ---------------------------------------------------------------------------
# Retry loop — successful cases
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestSendInBackgroundRetrySuccess:
    async def test_does_not_retry_on_first_attempt_success(self) -> None:
        """When the first `provider.send` returns success, no retries
        happen. No `send_failed` warning, no sleep."""
        from src.modules.auth.email_service import _send_in_background

        provider = MagicMock()
        provider.send = AsyncMock(return_value=_FakeResult(success=True))

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
            patch(
                "src.modules.auth.email_service.asyncio.sleep",
                AsyncMock(),
            ) as mock_sleep,
            patch("src.modules.auth.email_service.logger") as mock_logger,
        ):
            mock_settings.SMTP_RETRY_MAX_ATTEMPTS = 3
            mock_settings.LOG_INCLUDE_DEV_OTPS = False
            mock_settings.SMTP_FROM_NAME = "QlockCare"
            mock_settings.SMTP_FROM_EMAIL = "noreply@qlockcare.local"
            mock_scope.return_value = _null_scope()

            await _send_in_background(
                recipient_user_id=MagicMock(),
                message=_make_msg(),
                dev_otp_for_test_only=None,
                kind="otp",
            )

        provider.send.assert_called_once()
        mock_sleep.assert_not_called()
        # No retry-success log either — we never retried.
        info_calls = [
            call for call in mock_logger.info.call_args_list
            if call.args and "retry_succeeded" in str(call.args[0])
        ]
        assert info_calls == []


# ---------------------------------------------------------------------------
# Retry loop — failure recovery
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestSendInBackgroundRetryFailureRecovery:
    async def test_retries_then_succeeds(self) -> None:
        """First attempt fails (`success=False`), second succeeds.
        `provider.send` is called twice; sleep happens once between
        attempts; the `retry_succeeded` info log is emitted."""
        from src.modules.auth.email_service import _send_in_background

        provider = MagicMock()
        provider.send = AsyncMock(
            side_effect=[
                _FakeResult(success=False, error="connection refused"),
                _FakeResult(success=True),
            ]
        )

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
            patch(
                "src.modules.auth.email_service.asyncio.sleep",
                AsyncMock(),
            ) as mock_sleep,
            patch("src.modules.auth.email_service.logger") as mock_logger,
        ):
            mock_settings.SMTP_RETRY_MAX_ATTEMPTS = 3
            mock_settings.SMTP_RETRY_BASE_DELAY_SECONDS = 1.0
            mock_settings.SMTP_RETRY_MAX_DELAY_SECONDS = 10.0
            mock_settings.SMTP_RETRY_JITTER = 0.0
            mock_settings.LOG_INCLUDE_DEV_OTPS = False
            mock_settings.SMTP_FROM_NAME = "QlockCare"
            mock_settings.SMTP_FROM_EMAIL = "noreply@qlockcare.local"
            mock_scope.return_value = _null_scope()

            await _send_in_background(
                recipient_user_id=MagicMock(),
                message=_make_msg(),
                dev_otp_for_test_only=None,
                kind="otp",
            )

        assert provider.send.call_count == 2
        # Sleep happens once between attempts.
        mock_sleep.assert_called_once()
        # `retry_succeeded` log was emitted with attempt=2.
        info_calls = [
            call for call in mock_logger.info.call_args_list
            if call.args and "retry_succeeded" in str(call.args[0])
        ]
        assert len(info_calls) == 1
        assert info_calls[0].kwargs["attempt"] == 2


# ---------------------------------------------------------------------------
# Retry loop — give-up
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestSendInBackgroundGiveUp:
    async def test_gives_up_after_max_attempts(self) -> None:
        """All `SMTP_RETRY_MAX_ATTEMPTS` attempts fail → give up,
        log `send_exhausted` at error level. `provider.send` is
        called exactly `MAX_ATTEMPTS` times."""
        from src.modules.auth.email_service import _send_in_background

        provider = MagicMock()
        provider.send = AsyncMock(
            return_value=_FakeResult(success=False, error="connection refused")
        )

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
            patch(
                "src.modules.auth.email_service.asyncio.sleep",
                AsyncMock(),
            ) as mock_sleep,
            patch("src.modules.auth.email_service.logger") as mock_logger,
        ):
            mock_settings.SMTP_RETRY_MAX_ATTEMPTS = 3
            mock_settings.SMTP_RETRY_BASE_DELAY_SECONDS = 1.0
            mock_settings.SMTP_RETRY_MAX_DELAY_SECONDS = 10.0
            mock_settings.SMTP_RETRY_JITTER = 0.0
            mock_settings.LOG_INCLUDE_DEV_OTPS = False
            mock_settings.SMTP_FROM_NAME = "QlockCare"
            mock_settings.SMTP_FROM_EMAIL = "noreply@qlockcare.local"
            mock_scope.return_value = _null_scope()

            await _send_in_background(
                recipient_user_id=MagicMock(),
                message=_make_msg(),
                dev_otp_for_test_only=None,
                kind="reset",
            )

        # 3 attempts total (1 initial + 2 retries).
        assert provider.send.call_count == 3
        # 2 sleeps between attempts.
        assert mock_sleep.call_count == 2

        # `send_exhausted` was logged at error level.
        error_calls = [
            call for call in mock_logger.error.call_args_list
            if call.args and "send_exhausted" in str(call.args[0])
        ]
        assert len(error_calls) == 1
        assert error_calls[0].kwargs["attempts"] == 3
        assert error_calls[0].kwargs["error"] == "connection refused"

    async def test_max_attempts_one_disables_retries(self) -> None:
        """`SMTP_RETRY_MAX_ATTEMPTS=1` (the escape hatch) means
        one attempt, no sleep, no retry — same behaviour as the
        pre-retry code path."""
        from src.modules.auth.email_service import _send_in_background

        provider = MagicMock()
        provider.send = AsyncMock(
            return_value=_FakeResult(success=False, error="smtp down")
        )

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
            patch(
                "src.modules.auth.email_service.asyncio.sleep",
                AsyncMock(),
            ) as mock_sleep,
        ):
            mock_settings.SMTP_RETRY_MAX_ATTEMPTS = 1
            mock_settings.SMTP_RETRY_BASE_DELAY_SECONDS = 1.0
            mock_settings.SMTP_RETRY_MAX_DELAY_SECONDS = 10.0
            mock_settings.SMTP_RETRY_JITTER = 0.0
            mock_settings.LOG_INCLUDE_DEV_OTPS = False
            mock_settings.SMTP_FROM_NAME = "QlockCare"
            mock_settings.SMTP_FROM_EMAIL = "noreply@qlockcare.local"
            mock_scope.return_value = _null_scope()

            await _send_in_background(
                recipient_user_id=MagicMock(),
                message=_make_msg(),
                dev_otp_for_test_only=None,
                kind="invitation",
            )

        assert provider.send.call_count == 1
        mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# Retry loop — backstop on unexpected exception
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestSendInBackgroundBackstopException:
    async def test_treats_provider_exception_as_failed_attempt(self) -> None:
        """`EmailProvider.send` is contractually not supposed to
        raise, but if it does (a future bug, an `asyncio.CancelledError`,
        anything), the runner must treat it as one failed attempt —
        log a `send_raised` warning + keep retrying — and not
        propagate the exception."""
        from src.modules.auth.email_service import _send_in_background

        provider = MagicMock()
        # First attempt raises (a genuine surprise), second succeeds.
        provider.send = AsyncMock(
            side_effect=[
                RuntimeError("aiosmtplib crashed unexpectedly"),
                _FakeResult(success=True),
            ]
        )

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
            patch(
                "src.modules.auth.email_service.asyncio.sleep",
                AsyncMock(),
            ),
            patch("src.modules.auth.email_service.logger") as mock_logger,
        ):
            mock_settings.SMTP_RETRY_MAX_ATTEMPTS = 3
            mock_settings.SMTP_RETRY_BASE_DELAY_SECONDS = 1.0
            mock_settings.SMTP_RETRY_MAX_DELAY_SECONDS = 10.0
            mock_settings.SMTP_RETRY_JITTER = 0.0
            mock_settings.LOG_INCLUDE_DEV_OTPS = False
            mock_settings.SMTP_FROM_NAME = "QlockCare"
            mock_settings.SMTP_FROM_EMAIL = "noreply@qlockcare.local"
            mock_scope.return_value = _null_scope()

            # Must not raise.
            await _send_in_background(
                recipient_user_id=MagicMock(),
                message=_make_msg(),
                dev_otp_for_test_only=None,
                kind="otp",
            )

        assert provider.send.call_count == 2
        # `send_raised` was logged at warning level for the first attempt.
        warn_calls = [
            call for call in mock_logger.warning.call_args_list
            if call.args and "send_raised" in str(call.args[0])
        ]
        assert len(warn_calls) == 1
        assert warn_calls[0].kwargs["attempt"] == 1
        assert warn_calls[0].kwargs["error"] == "RuntimeError"


# ---------------------------------------------------------------------------
# Logging — fields and shape
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestSendInBackgroundLoggingFields:
    async def test_send_failed_log_includes_attempt_and_delay(self) -> None:
        """The `send_failed` warning log must include `attempt`,
        `max_attempts`, `delay_seconds`, `to`, and `error` so
        ops dashboards can alert on it precisely."""
        from src.modules.auth.email_service import _send_in_background

        provider = MagicMock()
        provider.send = AsyncMock(
            return_value=_FakeResult(success=False, error="connection refused")
        )

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
            patch(
                "src.modules.auth.email_service.asyncio.sleep",
                AsyncMock(),
            ),
            patch("src.modules.auth.email_service.logger") as mock_logger,
        ):
            mock_settings.SMTP_RETRY_MAX_ATTEMPTS = 2
            mock_settings.SMTP_RETRY_BASE_DELAY_SECONDS = 1.0
            mock_settings.SMTP_RETRY_MAX_DELAY_SECONDS = 10.0
            mock_settings.SMTP_RETRY_JITTER = 0.0
            mock_settings.LOG_INCLUDE_DEV_OTPS = False
            mock_settings.SMTP_FROM_NAME = "QlockCare"
            mock_settings.SMTP_FROM_EMAIL = "noreply@qlockcare.local"
            mock_scope.return_value = _null_scope()

            await _send_in_background(
                recipient_user_id=MagicMock(),
                message=_make_msg(to="alex@example.com"),
                dev_otp_for_test_only=None,
                kind="reset",
            )

        # One `send_failed` log per failed attempt, except the last
        # (which logs `send_exhausted` instead). With MAX_ATTEMPTS=2
        # and all failures, that's 1 `send_failed` warning.
        send_failed_calls = [
            call for call in mock_logger.warning.call_args_list
            if call.args and "send_failed" in str(call.args[0])
        ]
        assert len(send_failed_calls) == 1
        kwargs = send_failed_calls[0].kwargs
        assert kwargs["to"] == "alex@example.com"
        assert kwargs["attempt"] == 1
        assert kwargs["max_attempts"] == 2
        assert kwargs["delay_seconds"] == 1.0  # base, no jitter
        assert kwargs["error"] == "connection refused"


__all__ = [
    "TestComputeBackoffDelay",
    "TestSendInBackgroundBackstopException",
    "TestSendInBackgroundGiveUp",
    "TestSendInBackgroundLoggingFields",
    "TestSendInBackgroundRetryFailureRecovery",
    "TestSendInBackgroundRetrySuccess",
]
