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
    """Create a notification row for a single recipient.

    Returns None if an equivalent notification was already dispatched
    for the same recipient + type + entity_id within the last 60
    seconds (dedup window). This protects against double-fire from
    retry storms without needing a unique constraint.

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
    return int(result.rowcount or 0)


__all__ = [
    "dispatch_notification",
    "get_notification_or_404",
    "list_my_notifications",
    "mark_all_read",
    "mark_read",
]
