"""Notifications service — dispatcher + recipient-facing queries.

Two halves:
  1. `dispatch_notification(...)` is called by writers in other modules
     to enqueue a notification for a user. It does NOT commit — the
     caller controls the surrounding transaction. Dedup is handled
     by the DB layer (composite unique on `(recipient_user_id, type,
     metadata->>'entity_id')` is enforced by application code via a
     pre-insert check; for Phase 1 we use a simple recent-lookup).

  2. `list_my_notifications`, `mark_read`, `mark_all_read` — recipient
     endpoints. The recipient can only see + mark-read their own
     notifications; cross-user reads are blocked by RLS + the
     recipient_user_id check below.
"""

from __future__ import annotations

import uuid
from builtins import type as _type
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import NotFoundError
from src.modules.identity.dependencies import AuthContext
from src.modules.notifications.models import Notification
from src.shared.domain.enums import NotificationStatus, NotificationType


# --------------------------------------------------------------------------
# Dispatcher
# --------------------------------------------------------------------------
async def dispatch_notification(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    recipient_user_id: uuid.UUID,
    type: NotificationType,
    title: str,
    body: str,
    metadata: dict[str, Any] | None = None,
) -> Notification | None:
    """Create a notification row for a single recipient and fan out.

    Returns None in two cases:
      - dedup: an equivalent (recipient + type + entity_id) notification
        was already dispatched within the last 60 seconds.
      - opted-out: the recipient has explicitly opted out of
        `(type, IN_APP)`. The in-app row is the primary surface; if
        the user has opted out of it, we skip everything (no rows
        inserted; downstream channels are also skipped because their
        delivery rows would orphan without a parent notification).

    Otherwise:
      - Insert a `Notification` row with status SENT.
      - Fan out to other enabled channels via `dispatch_multichannel`.
        The multichannel call updates the row's status to DELIVERED
        (any channel succeeded) or FAILED (all attempted channels
        failed) before returning.

    Caller is responsible for committing the surrounding transaction.
    """
    metadata = metadata or {}
    entity_id = metadata.get("entity_id")
    if entity_id is not None:
        # Dedup: skip if there's an unread notification of the same
        # type + entity_id in the last 60 seconds.
        cutoff = datetime.now(UTC).timestamp() - 60
        existing = (
            await session.execute(
                select(Notification)
                .where(
                    Notification.recipient_user_id == recipient_user_id,
                    Notification.type == type,
                    Notification.metadata_["entity_id"].astext == str(entity_id),
                    Notification.read_at.is_(None),
                )
                .order_by(Notification.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None and existing.created_at.timestamp() >= cutoff:
            return None

    # Opt-out check for the IN_APP channel — the surface that holds the
    # notification row. If the user has opted out of in-app for this
    # type, we don't create any row (and therefore no other channels
    # are dispatched either).
    from src.modules.notifications.preferences import is_opted_in
    from src.shared.domain.enums import NotificationChannel

    if not await is_opted_in(
        session,
        user_id=recipient_user_id,
        type=type,
        channel=NotificationChannel.IN_APP,
    ):
        return None

    notification = Notification(
        agency_id=agency_id,
        recipient_user_id=recipient_user_id,
        type=type,
        title=title,
        body=body,
        status=NotificationStatus.SENT,
        metadata_=metadata,
    )
    session.add(notification)
    await session.flush()

    # Fan out to other enabled channels (EMAIL/SMS/etc.). Per-channel
    # try/except inside the dispatcher isolates provider crashes.
    from src.modules.notifications.deliveries import dispatch_multichannel

    try:
        await dispatch_multichannel(session, notification=notification)
    except Exception as exc:
        from src.core.logging import get_logger

        get_logger(__name__).error(
            "notifications.dispatch_multichannel_failed",
            notification_id=str(notification.id),
            error=_type(exc).__name__,
            detail=str(exc),
        )
        # Leave the row at SENT — the in-app delivery is real even if
        # the multichannel fan-out crashed.

    await session.flush()
    return notification


# --------------------------------------------------------------------------
# Recipient-facing queries
# --------------------------------------------------------------------------
async def list_my_notifications(
    session: AsyncSession,
    *,
    ctx: AuthContext,
    limit: int,
    cursor_created_at: datetime | None,
    cursor_id: uuid.UUID | None,
    unread_only: bool,
) -> tuple[list[Notification], str | None, int]:
    """Return one page of the caller's notifications.

    Returns (rows, next_cursor, unread_count).
    """
    base = select(Notification).where(
        Notification.recipient_user_id == ctx.user_id
    )
    if unread_only:
        base = base.where(Notification.read_at.is_(None))
    if cursor_created_at is not None and cursor_id is not None:
        base = base.where(
            (Notification.created_at < cursor_created_at)
            | (
                (Notification.created_at == cursor_created_at)
                & (Notification.id < cursor_id)
            )
        )
    base = (
        base.order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(limit + 1)  # fetch one extra to detect "more"
    )
    rows = list((await session.execute(base)).scalars().all())

    next_cursor: str | None = None
    if len(rows) > limit:
        last = rows[limit - 1]
        from src.shared.schemas.pagination import encode_cursor

        next_cursor = encode_cursor(created_at=last.created_at, id=last.id)
        rows = rows[:limit]

    unread_count = (
        await session.execute(
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.recipient_user_id == ctx.user_id,
                Notification.read_at.is_(None),
            )
        )
    ).scalar_one()

    return rows, next_cursor, int(unread_count)


async def get_notification_or_404(
    session: AsyncSession,
    *,
    notification_id: uuid.UUID,
    ctx: AuthContext,
) -> Notification:
    """Fetch a single notification, ensuring it belongs to the caller."""
    notif = (
        await session.execute(
            select(Notification).where(Notification.id == notification_id)
        )
    ).scalar_one_or_none()
    if notif is None or notif.recipient_user_id != ctx.user_id:
        # Return 404 (not 403) to avoid leaking the existence of other
        # users' notifications.
        raise NotFoundError("Notification not found.")
    return notif


async def mark_read(
    session: AsyncSession,
    *,
    notification_id: uuid.UUID,
    ctx: AuthContext,
) -> Notification:
    notif = await get_notification_or_404(
        session, notification_id=notification_id, ctx=ctx
    )
    if notif.read_at is None:
        notif.read_at = datetime.now(UTC)
        notif.status = NotificationStatus.READ
    await session.flush()
    return notif


async def mark_all_read(
    session: AsyncSession,
    *,
    ctx: AuthContext,
) -> int:
    """Mark every unread notification for the caller as read. Returns count."""
    now = datetime.now(UTC)
    result = await session.execute(
        update(Notification)
        .where(
            Notification.recipient_user_id == ctx.user_id,
            Notification.read_at.is_(None),
        )
        .values(read_at=now, status=NotificationStatus.READ)
    )
    # `rowcount` is on the underlying `CursorResult`; SQLAlchemy's async
    # `Result` exposes it but mypy can't see it through the generic.
    rowcount = getattr(result, "rowcount", 0) or 0
    return int(rowcount)


__all__ = [
    "dispatch_notification",
    "get_notification_or_404",
    "list_my_notifications",
    "mark_all_read",
    "mark_read",
]
