"""Transactional auth emails — OTP verify + password reset.

These are **not** domain notifications (visit check-in, appointment
reschedule, etc.). They are direct user-facing transactional emails
issued by the auth flow:

  - OTP email — sent after `accept-invitation` and `resend-otp`.
  - Password reset email — sent after `forgot-password`.

We deliberately skip the `notifications` table (no bell-icon entry for
transactional auth emails) and route directly through
`EmailProvider.send(...)`. The provider call is scheduled on
FastAPI's `BackgroundTasks` so an unreachable SMTP server cannot
block the request thread — same pattern as the just-shipped
`notifications/background.py:run_dispatch_in_background`.

Public API:
  - `send_otp_email(background_tasks, *, to_email, to_name, otp,
    expires_in_minutes)` — schedule an OTP email.
  - `send_password_reset_email(background_tasks, *, to_email,
    to_name, reset_token, expires_in_minutes)` — schedule a reset
    link email.

Both helpers build the `EmailMessage` synchronously and defer only
the network call. The deep-link URL in the body uses
`settings.FRONTEND_URL` so the SPA can deep-link to the verify/reset
page with the OTP / token pre-filled.

When SMTP is disabled (`SMTP_ENABLED=false`, the default in unit
tests), `EmailProvider.send` returns a `DeliveryResult(success=False)`
and the user is told "email sent" optimistically. Devs who need to
test the flow end-to-end without configuring SMTP can set
`LOG_INCLUDE_DEV_OTPS=true` — the OTP / reset token then appears in
the application log at INFO level under a clearly-labelled
`dev_*` field so production log scanners don't accidentally ingest
secrets. MUST stay False in production.
"""

from __future__ import annotations

import uuid
from email.message import EmailMessage
from typing import Final
from urllib.parse import urlencode

from fastapi import BackgroundTasks

from src.core.config import settings
from src.core.database import session_scope, set_session_context
from src.core.logging import get_logger

logger = get_logger(__name__)


_BRAND_NAME: Final[str] = "QlockCare"


def _from_address() -> str:
    """Build the From: header from settings."""
    return f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"


def _build_otp_email(
    *, to_email: str, to_name: str | None, otp: str, expires_in_minutes: int
) -> EmailMessage:
    """Build the OTP verification email.

    The body includes both the OTP code (so the user can paste it
    into the SPA) and a clickable deep link so they can verify with
    one tap from a phone.
    """
    query = urlencode({"email": to_email, "otp": otp})
    verify_url = f"{settings.FRONTEND_URL.rstrip('/')}/verify-email?{query}"

    greeting = f"Hi {to_name}," if to_name else "Hi,"
    body = (
        f"{greeting}\n\n"
        f"Welcome to {_BRAND_NAME}. Use the code below to verify your "
        f"email address — it expires in {expires_in_minutes} minutes.\n\n"
        f"  Verification code: {otp}\n\n"
        f"Or click this link to verify automatically:\n"
        f"  {verify_url}\n\n"
        f"If you didn't request this, you can safely ignore this email.\n\n"
        f"— The {_BRAND_NAME} team\n"
    )

    msg = EmailMessage()
    msg["From"] = _from_address()
    msg["To"] = to_email
    msg["Subject"] = f"Verify your {_BRAND_NAME} account"
    msg.set_content(body)
    return msg


def _build_reset_email(
    *,
    to_email: str,
    to_name: str | None,
    reset_token: str,
    expires_in_minutes: int,
) -> EmailMessage:
    """Build the password reset email."""
    query = urlencode({"token": reset_token})
    reset_url = f"{settings.FRONTEND_URL.rstrip('/')}/reset-password?{query}"

    greeting = f"Hi {to_name}," if to_name else "Hi,"
    body = (
        f"{greeting}\n\n"
        f"We received a request to reset the password on your {_BRAND_NAME} "
        f"account. Click the link below to choose a new password — it "
        f"expires in {expires_in_minutes} minutes.\n\n"
        f"  Reset your password: {reset_url}\n\n"
        f"If you didn't request a password reset, you can safely ignore "
        f"this email. Your password will not change unless you click the "
        f"link above.\n\n"
        f"— The {_BRAND_NAME} team\n"
    )

    msg = EmailMessage()
    msg["From"] = _from_address()
    msg["To"] = to_email
    msg["Subject"] = f"Reset your {_BRAND_NAME} password"
    msg.set_content(body)
    return msg


async def _send_in_background(
    *,
    recipient_user_id: uuid.UUID,
    message: EmailMessage,
    dev_otp_for_test_only: str | None,
    kind: str,
) -> None:
    """Run the actual `provider.send(...)` call off the request thread.

    Opens a fresh session via `session_scope()` and re-establishes a
    minimal RLS context (recipient user_id + a synthetic SYSTEM role)
    so any future reads of tenant-scoped tables inside the email
    pipeline satisfy RLS. The email-only path does not write any
    rows — `EmailProvider.send` is a pure network call — so this is
    belt-and-braces.

    The synthetic `SYSTEM` role does not match any of the policies
    in `alembic/versions/0008_notifications.py` /
    `0010_notifications_enhancements.py`. It is intentionally
    untrusted — it only matters if a future read-policy gates
    on `current_user_role`.

    Never raises — exceptions are logged at error level. A failed
    delivery row stays PENDING in our mental model: the request
    thread has already returned 202, the user sees "sent=true",
    and ops will see the failure in the application log.
    """
    # Dev escape hatch — log the OTP / reset token at INFO level so
    # local dev can complete the flow without configuring SMTP.
    # Logged with a clear "DEV ONLY" prefix and gated on
    # LOG_INCLUDE_DEV_OTPS so production log scanners do not ingest
    # secrets.
    if settings.LOG_INCLUDE_DEV_OTPS and dev_otp_for_test_only:
        logger.info(
            f"auth.email.dev_{kind}_for_test_only",
            extra={
                "dev_otp_for_test_only": dev_otp_for_test_only,
                "to": message["To"],
                "_dev_only": True,
            },
        )

    try:
        async with session_scope() as session:
            await set_session_context(
                session,
                user_id=str(recipient_user_id),
                agency_id=None,
                user_role="SYSTEM",
            )
            # Lazy import — EmailProvider is constructed by the
            # ProviderRegistry normally; we instantiate one directly
            # here because transactional auth emails do not go
            # through the multi-channel dispatcher.
            from src.modules.notifications.channels import EmailProvider

            provider = EmailProvider()
            await provider.send(
                to=str(message["To"]),
                subject=str(message["Subject"]),
                body=_body_from_message(message),
                metadata=None,
            )
    except Exception as exc:
        logger.error(
            f"auth.email.{kind}_send_failed",
            to=str(message["To"]),
            error=type(exc).__name__,
            detail=str(exc),
        )


def _body_from_message(msg: EmailMessage) -> str:
    """Return the plain-text body of an `EmailMessage`.

    `EmailMessage.get_content(...)` returns a `str` for text parts.
    We never build HTML in this module so the body is always
    plaintext.
    """
    payload = msg.get_content()
    if not isinstance(payload, str):
        # Defensive — if a future change adds HTML, fall back to the
        # raw bytes decoded best-effort.
        return payload.decode("utf-8", errors="replace")
    return payload


def send_otp_email(
    background_tasks: BackgroundTasks,
    *,
    to_email: str,
    to_name: str | None,
    otp: str,
    expires_in_minutes: int,
    recipient_user_id: uuid.UUID,
) -> None:
    """Schedule an OTP verification email to be sent after the response.

    The OTP/code is built synchronously (no I/O) so the request
    thread returns immediately. The actual SMTP call runs on
    FastAPI's `BackgroundTasks` after the HTTP response is flushed.
    """
    message = _build_otp_email(
        to_email=to_email,
        to_name=to_name,
        otp=otp,
        expires_in_minutes=expires_in_minutes,
    )
    background_tasks.add_task(
        _send_in_background,
        recipient_user_id=recipient_user_id,
        message=message,
        dev_otp_for_test_only=otp,
        kind="otp",
    )


def send_password_reset_email(
    background_tasks: BackgroundTasks,
    *,
    to_email: str,
    to_name: str | None,
    reset_token: str,
    expires_in_minutes: int,
    recipient_user_id: uuid.UUID,
) -> None:
    """Schedule a password-reset email to be sent after the response."""
    message = _build_reset_email(
        to_email=to_email,
        to_name=to_name,
        reset_token=reset_token,
        expires_in_minutes=expires_in_minutes,
    )
    background_tasks.add_task(
        _send_in_background,
        recipient_user_id=recipient_user_id,
        message=message,
        dev_otp_for_test_only=reset_token,
        kind="reset",
    )


__all__ = [
    "send_otp_email",
    "send_password_reset_email",
]
