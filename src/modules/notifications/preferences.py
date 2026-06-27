"""Notification preferences — per-(user, type, channel) opt-in/opt-out.

The default state is "opted-in to everything" — no rows exist for a
new user. Reads are lazy-seeded so the first `list_my_prefs` call
materialises a row for every `(type, channel)` combination and subsequent
calls return the user's stored preferences.

The dispatcher (`service.dispatch_notification`) consults `is_opted_in`
for the `(recipient, type, IN_APP)` row before inserting a notification.
For other channels, `dispatch_multichannel` looks up prefs for each
`(type, channel)` it plans to fan out to.

RLS:
  - `notification_preferences` has FORCE ROW LEVEL SECURITY with two
    policies: owner (user_id = current_user) has SELECT/INSERT/UPDATE/
    DELETE on their own rows; AGENCY_ADMIN can SELECT in their agency.
  - Because we INSERT via the recipient's session, the owner policy
    permits writes without bypass. AGENCY_ADMINs cannot modify.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.logging import get_logger
from src.modules.notifications.models import NotificationPreference
from src.shared.domain.enums import NotificationChannel, NotificationType

log = get_logger(__name__)


async def get_or_create_prefs(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    agency_id: uuid.UUID,
    type: NotificationType,
    channel: NotificationChannel,
) -> NotificationPreference:
    """Fetch one preference row, creating it (opted-in by default) if absent.

    Uses Postgres `INSERT ... ON CONFLICT DO NOTHING` + SELECT so we
    avoid a race when two requests read concurrently and both try to
    insert. The conflict target is the composite PK (user_id, type,
    channel).
    """
    insert_stmt = (
        pg_insert(NotificationPreference)
        .values(
            user_id=user_id,
            agency_id=agency_id,
            type=type,
            channel=channel,
            opted_in=True,
            updated_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(
            index_elements=["user_id", "type", "channel"]
        )
    )
    await session.execute(insert_stmt)

    row = (
        await session.execute(
            select(NotificationPreference).where(
                NotificationPreference.user_id == user_id,
                NotificationPreference.type == type,
                NotificationPreference.channel == channel,
            )
        )
    ).scalar_one()
    return row


async def set_pref(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    agency_id: uuid.UUID,
    type: NotificationType,
    channel: NotificationChannel,
    opted_in: bool,
) -> NotificationPreference:
    """Create-if-absent + flip opted_in for one (user, type, channel).

    Returns the persisted row. Caller is responsible for committing.
    """
    insert_stmt = (
        pg_insert(NotificationPreference)
        .values(
            user_id=user_id,
            agency_id=agency_id,
            type=type,
            channel=channel,
            opted_in=opted_in,
            updated_at=datetime.now(UTC),
        )
        .on_conflict_do_update(
            index_elements=["user_id", "type", "channel"],
            set_={
                "opted_in": opted_in,
                "updated_at": datetime.now(UTC),
            },
        )
    )
    await session.execute(insert_stmt)

    row = (
        await session.execute(
            select(NotificationPreference).where(
                NotificationPreference.user_id == user_id,
                NotificationPreference.type == type,
                NotificationPreference.channel == channel,
            )
        )
    ).scalar_one()
    return row


async def is_opted_in(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    type: NotificationType,
    channel: NotificationChannel,
) -> bool:
    """Return whether the user is opted in for `(type, channel)`.

    If no row exists, the user is considered opted-in (default-on).
    This is the hot-path check called by the dispatcher.
    """
    row = (
        await session.execute(
            select(NotificationPreference.opted_in).where(
                NotificationPreference.user_id == user_id,
                NotificationPreference.type == type,
                NotificationPreference.channel == channel,
            )
        )
    ).scalar_one_or_none()
    return True if row is None else bool(row)


async def list_my_prefs(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> list[NotificationPreference]:
    """Materialise one row per (type, channel) for the user.

    On the first call for a user, this INSERTs all `(14 types x 4 channels
    = 56)` rows with `opted_in = true` so subsequent reads return the
    user's stored state. Returns all rows ordered by `(type, channel)`.
    """
    # Step 1: bulk-insert any missing combos. ON CONFLICT DO NOTHING
    # means existing rows are left alone.
    rows_values = [
        {
            "user_id": user_id,
            "agency_id": agency_id,
            "type": t,
            "channel": c,
            "opted_in": True,
            "updated_at": datetime.now(UTC),
        }
        for t in NotificationType
        for c in (
            NotificationChannel.IN_APP,
            NotificationChannel.EMAIL,
            NotificationChannel.SMS,
            NotificationChannel.PUSH,
        )
    ]
    if rows_values:
        insert_stmt = (
            pg_insert(NotificationPreference)
            .values(rows_values)
            .on_conflict_do_nothing(
                index_elements=["user_id", "type", "channel"]
            )
        )
        await session.execute(insert_stmt)

    # Step 2: read everything back.
    result = (
        await session.execute(
            select(NotificationPreference)
            .where(NotificationPreference.user_id == user_id)
            .order_by(
                NotificationPreference.type,
                NotificationPreference.channel,
            )
        )
    ).scalars().all()
    return list(result)


__all__ = [
    "get_or_create_prefs",
    "is_opted_in",
    "list_my_prefs",
    "set_pref",
]
