"""Multi-channel notification dispatcher — split into two phases.

Phase 1 — `prepare_deliveries(session, notification)`:
  Synchronous. Inserts one PENDING `NotificationDelivery` row per
  available channel that the recipient is opted in to. Returns
  `[(channel, delivery_id), ...]`. Called inside the request thread so
  the in-app `Notification` row + per-channel delivery rows are durable
  before the response returns.

Phase 2 — `dispatch_provider_phase(session, notification, deliveries)`:
  Background. For each `(channel, delivery_id)` from Phase 1, calls
  `provider.send(...)` and UPDATEs the delivery row to DELIVERED or
  FAILED. Flips the parent `Notification.status` to DELIVERED if any
  channel succeeded, FAILED if all attempted channels failed.

The split exists because the provider calls (EMAIL → SMTP,
SMS → Twilio) are network-bound and can hang for tens of seconds when
the upstream is unreachable. By deferring Phase 2 to FastAPI's
`BackgroundTasks` (via `src/modules/notifications/background.py`), the
HTTP response returns immediately and the slow provider call runs
after the client has disconnected.

Phase 1 ordering:
  1. Caller (`service.dispatch_notification`) inserts the `Notification`
     row and flushes.
  2. `prepare_deliveries` runs in the same session, inserts one
     `NotificationDelivery` row per channel at status=PENDING, flushes.

Phase 2 ordering (in a background task on a fresh session):
  1. `run_dispatch_in_background` reloads the `Notification` row by id,
     establishes the actor's RLS context, and calls
     `dispatch_provider_phase`.
  2. For each delivery row: resolve the recipient's channel address
     (email/phone), call `provider.send(...)`, UPDATE delivery row to
     DELIVERED/FAILED.
  3. After all channels run, update parent `Notification.status` based
     on outcomes.
  4. Commit.

Per-channel try/except isolates provider crashes so one failing channel
doesn't abort the rest.
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


async def prepare_deliveries(
    session: AsyncSession,
    *,
    notification: Notification,
) -> list[tuple[NotificationChannel, uuid.UUID]]:
    """Phase 1 — insert one PENDING `NotificationDelivery` row per available
    channel that the recipient is opted in to.

    Returns the list of `(channel, delivery_id)` tuples that the
    background task should process in Phase 2.

    Channels that are not physically enabled in this environment
    (e.g. SMS when Twilio is not configured) are skipped, as are
    channels the recipient has explicitly opted out of.

    Called inside the request thread so the in-app `Notification` row
    + delivery rows are durable before the HTTP response returns.
    """
    available_channels = ProviderRegistry.enabled_channels()
    deliveries: list[tuple[NotificationChannel, uuid.UUID]] = []

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
        deliveries.append((channel, delivery_id))

    return deliveries


async def dispatch_provider_phase(
    session: AsyncSession,
    *,
    notification: Notification,
    deliveries: list[tuple[NotificationChannel, uuid.UUID]],
) -> None:
    """Phase 2 — call each provider and UPDATE the delivery row to
    DELIVERED/FAILED. Runs in a background task.

    Updates `notification.status` based on outcomes:
      - Any channel DELIVERED → status=DELIVERED
      - All attempted channels FAILED → status=FAILED
      - No channels attempted (empty `deliveries` list) → status
        unchanged (row stays SENT).

    Per-channel try/except isolates provider crashes. Programmer
    errors propagate (we don't want to swallow them silently).
    """
    any_succeeded = False
    any_attempted = False

    for channel, delivery_id in deliveries:
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

        provider: NotificationProvider | None = ProviderRegistry.get(channel)
        if provider is None:
            # Provider was removed between Phase 1 and Phase 2 — mark
            # FAILED with a clear reason.
            await _update_delivery_status(
                session,
                delivery_id=delivery_id,
                status_value=NotificationStatus.FAILED,
                provider_message_id=None,
                error="Provider unavailable",
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
    # else: no channel attempted — leave the row at SENT.


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


__all__ = ["dispatch_provider_phase", "prepare_deliveries"]
