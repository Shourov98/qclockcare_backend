"""Broadcast — agency-wide announcement fan-out.

`broadcast_to_agency(session, *, agency_id, sender, request)` inserts one
`Notification` row per active user in the agency (excluding the sender
themselves) and returns dispatched / skipped_opted_out / failed counts.

Per-recipient IN_APP prefs are consulted before each insert. EMAIL/SMS
channels are skipped at this layer — broadcast is in-app only (the
`channel_filter` on `BroadcastRequest` is reserved for future use; this
keeps the blast from accidentally emailing thousands of users at once).

The function does NOT commit; the caller controls the transaction.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import ValidationError
from src.core.logging import get_logger
from src.modules.identity.models import User, UserRoleAssignment
from src.modules.notifications.models import Notification
from src.modules.notifications.preferences import is_opted_in
from src.modules.notifications.schemas import BroadcastRequest
from src.shared.domain.enums import (
    NotificationChannel,
    NotificationStatus,
    UserRole,
    UserStatus,
)

log = get_logger(__name__)


async def broadcast_to_agency(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    sender_user_id: uuid.UUID,
    request: BroadcastRequest,
) -> tuple[int, int, int]:
    """Insert one notification per ACTIVE user in the agency.

    Returns (dispatched, skipped_opted_out, failed).

    `dispatched` = rows successfully inserted.
    `skipped_opted_out` = users who opted out of IN_APP for this type.
    `failed` = users where the insert raised (best-effort: we log and
               continue so one bad row doesn't kill the broadcast).
    """
    if not request.title.strip():
        raise ValidationError("Broadcast title cannot be empty.")
    if not request.body.strip():
        raise ValidationError("Broadcast body cannot be empty.")

    # Active users in the agency — joined through user_roles. Excludes
    # INVITED / INACTIVE / LOCKED / ARCHIVED.
    user_rows = (
        await session.execute(
            select(User.id, User.status)
            .join(
                UserRoleAssignment,
                UserRoleAssignment.user_id == User.id,
            )
            .where(
                UserRoleAssignment.agency_id == agency_id,
                User.status == UserStatus.ACTIVE,
            )
            .distinct()
        )
    ).all()

    dispatched = 0
    skipped_opted_out = 0
    failed = 0

    for user_id, _status in user_rows:
        if user_id == sender_user_id:
            # Don't notify yourself about your own broadcast.
            continue
        try:
            opted_in = await is_opted_in(
                session,
                user_id=user_id,
                type=request.type,
                channel=NotificationChannel.IN_APP,
            )
            if not opted_in:
                skipped_opted_out += 1
                continue

            notification = Notification(
                agency_id=agency_id,
                recipient_user_id=user_id,
                type=request.type,
                title=request.title,
                body=request.body,
                status=NotificationStatus.SENT,
                metadata_={
                    **request.metadata,
                    "broadcast": True,
                },
            )
            session.add(notification)
            dispatched += 1
        except Exception as exc:
            log.warning(
                "notifications.broadcast.row_failed",
                recipient=str(user_id),
                error=type(exc).__name__,
                detail=str(exc),
            )
            failed += 1

    await session.flush()
    return dispatched, skipped_opted_out, failed


def resolve_broadcast_agency(
    *,
    ctx_agency_id: uuid.UUID | None,
    ctx_role: UserRole,
    requested_agency_id: uuid.UUID | None,
) -> uuid.UUID:
    """Pick which agency the broadcast is scoped to.

    - SUPER_ADMIN may target any agency via `?agency_id=...`. If they
      omit it, the broadcast is rejected (you can't blast the whole
      multi-tenant platform from one click).
    - AGENCY_ADMIN can only target their own agency; `?agency_id=...`
      is ignored.
    - Any other role is rejected upstream by `require_role`.
    """
    if ctx_role == UserRole.SUPER_ADMIN:
        if requested_agency_id is None:
            raise ValidationError(
                "SUPER_ADMIN must specify ?agency_id=... for broadcast."
            )
        return requested_agency_id
    if ctx_agency_id is None:
        raise ValidationError("Cannot resolve agency for broadcast.")
    return ctx_agency_id


__all__ = [
    "broadcast_to_agency",
    "resolve_broadcast_agency",
]
