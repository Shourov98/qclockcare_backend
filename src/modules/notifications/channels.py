"""Pluggable notification providers — one per channel.

Phase 1 covers:
  - IN_APP: writes to the `notifications` table (already done by
    `dispatch_notification`). The provider here is a thin wrapper that
    marks a `NotificationDelivery` row as DELIVERED once the in-app
    row is committed.
  - EMAIL: real SMTP via aiosmtplib, controlled by `SMTP_ENABLED`.
  - SMS: stub provider. When `SMS_ENABLED=false`, logs the message and
    returns success=True so the dispatch loop completes. When
    `SMS_ENABLED=true` and Twilio creds are missing, raises
    NotImplementedError so ops knows the feature isn't wired.

Future phases can add PUSH (FCM/APNS) by implementing `NotificationProvider`.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, ClassVar

import aiosmtplib

from src.core.config import settings
from src.core.logging import get_logger
from src.shared.domain.enums import NotificationChannel

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """Provider response for one send attempt.

    - `success=True` means the provider accepted the message (SMTP 250,
      Twilio 201, etc.). It does NOT guarantee the recipient saw it.
    - `provider_message_id` is the provider's tracking id if any
      (SMTP does not provide one; we leave it None).
    - `error` is a short human-readable message; full tracebacks stay
      in the application log only.
    """

    success: bool
    provider_message_id: str | None = None
    error: str | None = None


class NotificationProvider(abc.ABC):
    """Abstract base for channel-specific senders.

    Each provider is stateless and safe to share across requests.
    `send` raises only for programmer errors (misconfiguration); all
    expected send failures (network, auth, bad recipient) must return
    a `DeliveryResult(success=False, ...)` instead of raising, so the
    dispatcher can keep going on other channels.
    """

    channel: ClassVar[NotificationChannel]

    @abc.abstractmethod
    async def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        metadata: dict[str, Any] | None = None,
    ) -> DeliveryResult:
        """Attempt to deliver one message. Must not raise on send failures."""


class InAppProvider(NotificationProvider):
    """The in-app channel is always 'delivered' once the row commits.

    The actual `notifications` row insert happens upstream in
    `dispatch_notification` before this provider is invoked. This
    provider just records the success so a `NotificationDelivery` row
    can be stamped DELIVERED.
    """

    channel = NotificationChannel.IN_APP

    async def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        metadata: dict[str, Any] | None = None,
    ) -> DeliveryResult:
        # `to` for in-app is the user_id (str-encoded UUID). The
        # notification row already exists by the time this runs.
        return DeliveryResult(success=True, provider_message_id=f"in-app:{to}")


class EmailProvider(NotificationProvider):
    """SMTP provider — uses aiosmtplib for async delivery."""

    channel = NotificationChannel.EMAIL

    async def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        metadata: dict[str, Any] | None = None,
    ) -> DeliveryResult:
        if not settings.SMTP_ENABLED:
            return DeliveryResult(
                success=False,
                error="SMTP disabled (set SMTP_ENABLED=true to deliver email)",
            )

        message = EmailMessage()
        message["From"] = (
            f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"
        )
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)

        try:
            await aiosmtplib.send(
                message,
                hostname=settings.SMTP_HOST,
                port=settings.SMTP_PORT,
                username=settings.SMTP_USERNAME or None,
                password=(
                    settings.SMTP_PASSWORD.get_secret_value()
                    if settings.SMTP_PASSWORD
                    else None
                ),
                use_tls=settings.SMTP_USE_TLS,
                timeout=settings.SMTP_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            log.warning(
                "notifications.email_send_failed",
                to=to,
                error=type(exc).__name__,
                detail=str(exc),
            )
            return DeliveryResult(success=False, error=str(exc))

        return DeliveryResult(success=True)


class SMSProvider(NotificationProvider):
    """SMS provider — stub for Phase 1.

    Returns success=True and logs the message when `SMS_ENABLED=false`
    (the default — no Twilio creds in dev). Raises `NotImplementedError`
    on instantiation when `SMS_ENABLED=true` and creds are missing so
    ops sees the missing-config error at startup rather than at send time.
    """

    channel = NotificationChannel.SMS

    def __init__(self) -> None:
        if settings.SMS_ENABLED:
            missing = []
            if not settings.TWILIO_ACCOUNT_SID:
                missing.append("TWILIO_ACCOUNT_SID")
            if not settings.TWILIO_AUTH_TOKEN:
                missing.append("TWILIO_AUTH_TOKEN")
            if not settings.TWILIO_FROM_NUMBER:
                missing.append("TWILIO_FROM_NUMBER")
            if missing:
                raise NotImplementedError(
                    "SMS_ENABLED=true but Twilio is not configured. "
                    f"Missing: {', '.join(missing)}. "
                    "Set SMS_ENABLED=false or wire a real Twilio provider."
                )

    async def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        metadata: dict[str, Any] | None = None,
    ) -> DeliveryResult:
        # Phase 1 stub — log and succeed.
        log.info(
            "notifications.sms_stub_send",
            to=to,
            body_len=len(body),
        )
        return DeliveryResult(success=True, provider_message_id=f"sms-stub:{to}")


class ProviderRegistry:
    """Singleton registry — instantiates one provider per channel on first use.

    Providers are cheap and stateless; we cache them on the module-level
    `_PROVIDERS` dict. IN_APP + SMS are always present; EMAIL is only
    present when `SMTP_ENABLED=true`. A provider missing for a channel
    means the channel is disabled in this environment.
    """

    _PROVIDERS: ClassVar[dict[NotificationChannel, NotificationProvider]] = {}

    @classmethod
    def get(cls, channel: NotificationChannel) -> NotificationProvider | None:
        if channel not in cls._PROVIDERS:
            if channel == NotificationChannel.IN_APP:
                cls._PROVIDERS[channel] = InAppProvider()
            elif channel == NotificationChannel.EMAIL:
                if settings.SMTP_ENABLED:
                    cls._PROVIDERS[channel] = EmailProvider()
                # else: leave un-cached — EMAIL is disabled in this env
            elif channel == NotificationChannel.SMS:
                try:
                    cls._PROVIDERS[channel] = SMSProvider()
                except NotImplementedError:
                    log.warning("notifications.sms_provider_unconfigured")
            elif channel == NotificationChannel.PUSH:
                log.warning("notifications.push_provider_unimplemented")
        return cls._PROVIDERS.get(channel)

    @classmethod
    def enabled_channels(cls) -> list[NotificationChannel]:
        """Channels that have a usable provider right now."""
        return [
            ch
            for ch in NotificationChannel
            if ch in {NotificationChannel.IN_APP, NotificationChannel.SMS}
            or (ch == NotificationChannel.EMAIL and settings.SMTP_ENABLED)
        ]


__all__ = [
    "DeliveryResult",
    "EmailProvider",
    "InAppProvider",
    "NotificationProvider",
    "ProviderRegistry",
    "SMSProvider",
]
