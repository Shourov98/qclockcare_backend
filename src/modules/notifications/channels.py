"""Pluggable notification providers — one per channel.

Phase 1 covers:
  - IN_APP: writes to the `notifications` table (already done by
    `dispatch_notification`). The provider here is a thin wrapper that
    marks a `NotificationDelivery` row as DELIVERED once the in-app
    row is committed.
  - EMAIL: real delivery via Resend (ADR-0020) when `RESEND_ENABLED=true`
    and `RESEND_API_KEY` is set; otherwise real SMTP via aiosmtplib when
    `SMTP_ENABLED=true`; otherwise dev-log fallback.
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
import httpx

from src.core.config import settings
from src.core.logging import get_logger
from src.shared.domain.enums import NotificationChannel

log = get_logger(__name__)

# Process-local dedupe of dev-mode email logs. When SMTP_ENABLED=false the
# EmailProvider logs the full rendered email (to / from / subject / body)
# so the developer can complete the auth flow without a real SMTP server.
# The retry loop in `auth/email_service.py:_send_in_background` calls
# `EmailProvider.send` up to three times per attempt; without dedupe the
# same email is logged three times in the application log. Keyed by
# (recipient, subject) so two different invites to the same recipient
# (e.g. staff invite vs password reset) still both log.
#
# Process-local and intentionally never cleared: in tests we mock
# `EmailProvider.send` so this branch doesn't run, and in production
# SMTP_ENABLED is true so the dev branch is gated out entirely.
_DEV_LOGGED_EMAILS: set[tuple[str, str]] = set()


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
    """Transactional email provider.

    Delivery order (ADR-0020):

    1. **Resend** — when `RESEND_ENABLED=true` AND `RESEND_API_KEY` is
       set. POSTs to `https://api.resend.com/emails` via httpx.
       The Resend branch wins over SMTP because it's the project's
       preferred delivery path — better deliverability than generic
       SMTP relays, and the env validator refuses to start the app
       with `RESEND_ENABLED=true` and an empty key.
    2. **SMTP** — when `SMTP_ENABLED=true`. Uses aiosmtplib. Kept for
       self-hosted deployments that prefer to relay through their own
       mail server.
    3. **Dev-log fallback** — when neither is enabled. Logs the full
       rendered email at INFO so local devs can complete the auth
       flow without configuring either provider.

    The `_DEV_LOGGED_EMAILS` set is shared across all branches below —
    only branch (3) writes to it, but the dedupe key is the same.
    """

    channel = NotificationChannel.EMAIL

    async def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        metadata: dict[str, Any] | None = None,
    ) -> DeliveryResult:
        resend_active = bool(
            settings.RESEND_ENABLED and settings.RESEND_API_KEY
        )
        smtp_active = bool(settings.SMTP_ENABLED)

        if not (resend_active or smtp_active):
            # Dev escape hatch — when both Resend and SMTP are disabled
            # (the default in local dev / unit tests), log the full
            # rendered email so the caller can complete the flow
            # without a real provider. Gated on APP_ENV != "production"
            # so production never logs PII/PHI even if both flags are
            # accidentally left off.
            #
            # Deduped by (to, subject) so the retry loop in
            # `auth/email_service.py:_send_in_background` doesn't spam
            # the log with three copies of the same email. The set is
            # process-local and intentionally never cleared — in tests
            # we mock `EmailProvider.send` so this branch doesn't run.
            if settings.APP_ENV != "production":
                dedupe_key = (to, subject)
                if dedupe_key not in _DEV_LOGGED_EMAILS:
                    _DEV_LOGGED_EMAILS.add(dedupe_key)
                    log.info(
                        "notifications.email_dev_log",
                        to=to,
                        from_addr=f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>",
                        subject=subject,
                        body=body,
                        _dev_only=True,
                    )
            return DeliveryResult(
                success=False,
                error=(
                    "Email disabled (set RESEND_ENABLED=true with "
                    "RESEND_API_KEY, or SMTP_ENABLED=true, to deliver email)"
                ),
            )

        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)

        # Resend path takes precedence over SMTP when both are on.
        # `RESEND_ENABLED=true` without a key is rejected at startup by
        # `_resend_enabled_needs_key`, so we don't need to re-check the
        # key here.
        if resend_active:
            message["From"] = (
                f"{settings.SMTP_FROM_NAME} <{settings.RESEND_EMAIL}>"
            )
            return await _send_via_resend(message, body)

        # SMTP path.
        message["From"] = (
            f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"
        )
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


async def _send_via_resend(
    message: EmailMessage, body: str
) -> DeliveryResult:
    """POST one transactional email through Resend's `/emails` endpoint.

    The Resend API accepts a JSON body with `from`, `to` (string or
    list), `subject`, and either `text` or `html`. We send `text`
    because every template in `auth/email_service.py` is plain text
    today — promoting any of them to HTML is a separate decision.

    Returns `DeliveryResult(success=True, provider_message_id=<id>)`
    on a 2xx response. Resend returns `{"id": "<uuid>"}` on success;
    we surface that id so the retry-success log can include it.

    Failure modes mapped to `DeliveryResult(success=False, ...)`:
      - non-2xx HTTP status — surfaces the upstream status + body so
        ops can grep for it.
      - network / timeout exception — same shape as the SMTP branch.

    Never raises; the `_send_in_background` retry loop relies on the
    `success=False` return to trigger a backoff. Raises here would
    short-circuit the retry contract documented on
    `NotificationProvider.send`.
    """
    recipient = str(message["To"])
    from_addr = str(message["From"])
    subject = str(message["Subject"])

    api_key = settings.RESEND_API_KEY
    if api_key is None:  # pragma: no cover — guarded by config validator
        return DeliveryResult(
            success=False,
            error="RESEND_API_KEY missing at runtime",
        )

    payload = {
        "from": from_addr,
        "to": [recipient],
        "subject": subject,
        "text": body,
    }
    headers = {
        "Authorization": f"Bearer {api_key.get_secret_value()}",
        "Content-Type": "application/json",
        # Resend echoes the `Idempotency-Key` header on retries; using
        # a stable key would let us coalesce duplicate background-task
        # dispatches. Today the auth retry loop sends distinct payloads
        # (different recipients), so we omit the header and rely on
        # the From/To/Subject triple as the natural dedupe key.
    }

    try:
        async with httpx.AsyncClient(
            timeout=settings.RESEND_API_TIMEOUT_SECONDS
        ) as client:
            response = await client.post(
                "https://api.resend.com/emails",
                json=payload,
                headers=headers,
            )
    except Exception as exc:
        log.warning(
            "notifications.email_resend_send_failed",
            to=recipient,
            error=type(exc).__name__,
            detail=str(exc),
        )
        return DeliveryResult(success=False, error=str(exc))

    if 200 <= response.status_code < 300:
        provider_message_id: str | None = None
        try:
            provider_message_id = response.json().get("id")
        except (ValueError, AttributeError):
            # Response wasn't JSON or had no `id` field — unusual for
            # Resend but not fatal. The caller still sees
            # `success=True`.
            provider_message_id = None
        return DeliveryResult(
            success=True,
            provider_message_id=f"resend:{provider_message_id}"
            if provider_message_id
            else "resend",
        )

    log.warning(
        "notifications.email_resend_non_2xx",
        to=recipient,
        status=response.status_code,
        body=response.text[:512],
    )
    return DeliveryResult(
        success=False,
        error=f"Resend {response.status_code}: {response.text[:256]}",
    )


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
    present when `RESEND_ENABLED=true` (with a key) OR `SMTP_ENABLED=true`.
    A provider missing for a channel means the channel is disabled in
    this environment.
    """

    _PROVIDERS: ClassVar[dict[NotificationChannel, NotificationProvider]] = {}

    @classmethod
    def get(cls, channel: NotificationChannel) -> NotificationProvider | None:
        if channel not in cls._PROVIDERS:
            if channel == NotificationChannel.IN_APP:
                cls._PROVIDERS[channel] = InAppProvider()
            elif channel == NotificationChannel.EMAIL:
                resend_active = bool(
                    settings.RESEND_ENABLED and settings.RESEND_API_KEY
                )
                if resend_active or settings.SMTP_ENABLED:
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
        resend_active = bool(
            settings.RESEND_ENABLED and settings.RESEND_API_KEY
        )
        email_active = resend_active or settings.SMTP_ENABLED
        return [
            ch
            for ch in NotificationChannel
            if ch in {NotificationChannel.IN_APP, NotificationChannel.SMS}
            or (ch == NotificationChannel.EMAIL and email_active)
        ]


__all__ = [
    "DeliveryResult",
    "EmailProvider",
    "InAppProvider",
    "NotificationProvider",
    "ProviderRegistry",
    "SMSProvider",
]
