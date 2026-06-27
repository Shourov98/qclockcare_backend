"""Multi-channel notification dispatcher.

For one already-inserted `Notification` row, fans out to the set of
channels the recipient is opted in to and writes one
`NotificationDelivery` row per attempt.

Ordering:
  1. Caller (e.g. `service.dispatch_notification`) has inserted the
     `Notification` row and flushed.
  2. `dispatch_multichannel(session, notification=row)` is called in
     the same session.
  3. We look up prefs for `(recipient_user_id, type, channel)` for
     each channel in `ProviderRegistry.enabled_channels()` plus IN_APP
     (IN_APP is always enabled).
  4. For each opted-in channel, call the provider and write a
     `NotificationDelivery` row capturing success/failure.
  5. The `Notification.status` is updated to `DELIVERED` if at least
     one channel succeeded, `FAILED` otherwise.

In-band only for Phase 1 — no background retry, no webhook callbacks.
Per-channel try/except ensures a provider crash can't kill the whole
dispatch; the next channel still runs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.logging import get_logger
from src.modules.notifications.channels import (
    NotificationProvider,
    ProviderRegistry,
)
from src.modules.notifications.models import (
    Notification,
    NotificationDelivery,
    NotificationPreference,
)
from src.shared.domain.enums import (
    NotificationChannel,
    NotificationStatus,
)

log = get_logger(__name__)


async def _delivery_row_id(
    session: AsyncSession,
    *,
    notification_id: uuid.UUID,
    channel: NotificationChannel,
) -> uuid.UUID:
    """Return the delivery row id for (notification_id, channel).

    The dispatcher always inserts a row before invoking the provider,
    so this should always find a row. Falls back to a new UUID if the
    row is unexpectedly absent (programmer error, logged loudly).
    """
    row = (
        await session.execute(
            select(NotificationDelivery.id).where(
                NotificationDelivery.notification_id == notification_id,
                NotificationDelivery.channel == channel,
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    log.error(
        "notifications.deliveries.row_missing",
        notification_id=str(notification_id),
        channel=channel.value,
    )
    return uuid.uuid4()


async def _channel_opted_in(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    type_: Any,
    channel: NotificationChannel,
) -> bool:
    """Hot-path check — defaults to opted-in if no row exists."""
    row = (
        await session.execute(
            select(NotificationPreference.opted_in).where(
                NotificationPreference.user_id == user_id,
                NotificationPreference.type == type_,
                NotificationPreference.channel == channel,
            )
        )
    ).scalar_one_or_none()
    return True if row is None else bool(row)


async def _insert_delivery_row(
    session: AsyncSession,
    *,
    notification: Notification,
    channel: NotificationChannel,
) -> None:
    """Insert a PENDING delivery row for (notification, channel).

    Unique constraint on (notification_id, channel) makes this idempotent
    across retries — a second call is a no-op.
    """
    stmt = (
        pg_insert(NotificationDelivery)
        .values(
            id=uuid.uuid4(),
            notification_id=notification.id,
            agency_id=notification.agency_id,
            channel=channel,
            status=NotificationStatus.PENDING,
            created_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(
            index_elements=["notification_id", "channel"]
        )
    )
    await session.execute(stmt)


async def _update_delivery_status(
    session: AsyncSession,
    *,
    delivery_id: uuid.UUID,
    status_value: NotificationStatus,
    provider_message_id: str | None,
    error: str | None,
) -> None:
    """Stamp a delivery row with the provider result."""
    from sqlalchemy import update

    await session.execute(
        update(NotificationDelivery)
        .where(NotificationDelivery.id == delivery_id)
        .values(
            status=status_value,
            provider_message_id=provider_message_id,
            error=error,
            delivered_at=(
                datetime.now(UTC)
                if status_value == NotificationStatus.DELIVERED
                else None
            ),
        )
    )


async def dispatch_multichannel(
    session: AsyncSession,
    *,
    notification: Notification,
) -> None:
    """Fan out one notification to every opted-in channel.

    Updates `notification.status` based on outcomes:
      - Any channel DELIVERED → status=DELIVERED
      - All opted-in channels FAILED → status=FAILED
      - User opted out of every channel → status unchanged
        (the row stays SENT — the caller already marked it SENT, and
        the in-app "delivery" is the row insert itself).

    Per-channel try/except isolates provider crashes. Programmer
    errors propagate (we don't want to swallow them silently).
    """
    # Channels that are *physically* available in this environment.
    available_channels = ProviderRegistry.enabled_channels()

    any_succeeded = False
    any_attempted = False

    for channel in available_channels:
        opted_in = await _channel_opted_in(
            session,
            user_id=notification.recipient_user_id,
            type_=notification.type,
            channel=channel,
        )
        if not opted_in:
            continue

        provider: NotificationProvider | None = ProviderRegistry.get(channel)
        if provider is None:
            continue

        await _insert_delivery_row(session, notification=notification, channel=channel)
        await session.flush()
        delivery_id = await _delivery_row_id(
            session,
            notification_id=notification.id,
            channel=channel,
        )

        # Build the "to" address per channel.
        to_address = await _resolve_recipient_address(
            session,
            user_id=notification.recipient_user_id,
            channel=channel,
        )
        if to_address is None:
            await _update_delivery_status(
                session,
                delivery_id=delivery_id,
                status_value=NotificationStatus.FAILED,
                provider_message_id=None,
                error=f"No {channel.value} address on file",
            )
            any_attempted = True
            continue

        try:
            result = await provider.send(
                to=to_address,
                subject=notification.title,
                body=notification.body,
                metadata=notification.metadata_,
            )
        except Exception as exc:
            log.error(
                "notifications.dispatch.provider_crashed",
                notification_id=str(notification.id),
                channel=channel.value,
                error=type(exc).__name__,
                detail=str(exc),
            )
            await _update_delivery_status(
                session,
                delivery_id=delivery_id,
                status_value=NotificationStatus.FAILED,
                provider_message_id=None,
                error=f"{type(exc).__name__}: {exc}",
            )
            any_attempted = True
            continue

        any_attempted = True
        if result.success:
            any_succeeded = True
            await _update_delivery_status(
                session,
                delivery_id=delivery_id,
                status_value=NotificationStatus.DELIVERED,
                provider_message_id=result.provider_message_id,
                error=None,
            )
        else:
            await _update_delivery_status(
                session,
                delivery_id=delivery_id,
                status_value=NotificationStatus.FAILED,
                provider_message_id=result.provider_message_id,
                error=result.error or "Unknown provider error",
            )

    # Update the parent notification's status based on fan-out outcome.
    if any_succeeded:
        notification.status = NotificationStatus.DELIVERED
    elif any_attempted:
        notification.status = NotificationStatus.FAILED
    # else: no channel attempted (user opted out of everything) — leave
    # the row at SENT, which is the caller's pre-multichannel default.


async def _resolve_recipient_address(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    channel: NotificationChannel,
) -> str | None:
    """Return the address the provider should send to.

    For IN_APP the "address" is just the user_id (the InAppProvider
    returns success regardless — the actual row write happens upstream).
    For EMAIL we need the user's email. For SMS we need a phone number
    on the User row (None for users without a phone on file).
    """
    from src.modules.identity.models import User

    if channel == NotificationChannel.IN_APP:
        return str(user_id)

    if channel == NotificationChannel.EMAIL:
        row = (
            await session.execute(select(User.email).where(User.id == user_id))
        ).scalar_one_or_none()
        return row

    if channel == NotificationChannel.SMS:
        # Phone is stored on the User row directly. If the user has
        # no phone on file, return None so the dispatcher stamps the
        # delivery FAILED with a clear "no phone on file" reason.
        phone = (
            await session.execute(select(User.phone).where(User.id == user_id))
        ).scalar_one_or_none()
        return phone

    # PUSH and any future channel — not wired in Phase 1.
    return None


__all__ = ["dispatch_multichannel"]
