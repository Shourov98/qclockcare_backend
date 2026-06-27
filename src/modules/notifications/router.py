"""Notifications router — `/notifications` recipient + admin endpoints.

Recipient endpoints (caller-scoped):
  GET    /notifications                       — list caller's notifications (cursor paginated)
  GET    /notifications/badge                 — cheap unread-count for bell icon
  GET    /notifications/preferences           — list caller's per-(type, channel) prefs
  PUT    /notifications/preferences/{type}/{channel} — toggle one pref
  GET    /notifications/{id}                  — single notification
  PATCH  /notifications/{id}/read             — mark one as read
  POST   /notifications/read-all              — mark all as read (returns count)

Admin endpoints (AGENCY_ADMIN / SUPER_ADMIN only):
  POST   /notifications/broadcast             — fan out one notice to every ACTIVE
                                                user in the agency

Per-recipient INSERTs are NOT exposed via HTTP — they happen through
the dispatcher (`notifications_service.dispatch_notification`) called
from other modules (visits, appointments, etc). The broadcast endpoint
is the one HTTP-driven writer (multi-recipient by design).
"""

from __future__ import annotations

import uuid
from builtins import type as _type
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.logging import get_logger
from src.modules.audit_logs.service import audit_log, request_ip_ua
from src.modules.identity.dependencies import (
    CurrentAuth,
    get_session_with_auth,
    require_role,
)
from src.modules.notifications import service as notifications_service
from src.modules.notifications.broadcast import (
    broadcast_to_agency,
    resolve_broadcast_agency,
)
from src.modules.notifications.models import Notification
from src.modules.notifications.preferences import list_my_prefs, set_pref
from src.modules.notifications.schemas import (
    BroadcastRequest,
    BroadcastResponse,
    NotificationBadgeResponse,
    NotificationListResponse,
    NotificationPreferenceResponse,
    NotificationPreferenceUpdateRequest,
    NotificationResponse,
)
from src.shared.domain.enums import (
    AuditAction,
    NotificationChannel,
    NotificationType,
    UserRole,
)
from src.shared.schemas.pagination import decode_cursor

router = APIRouter(prefix="/notifications", tags=["notifications"])
log = get_logger(__name__)


# --------------------------------------------------------------------------
# Recipient endpoints
# --------------------------------------------------------------------------
@router.get("", response_model=NotificationListResponse)
async def list_my_notifications_endpoint(
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    unread_only: Annotated[bool, Query()] = False,
) -> NotificationListResponse:
    """List the caller's notifications, newest first."""
    cursor_created_at: datetime | None = None
    cursor_id: uuid.UUID | None = None
    if cursor is not None:
        try:
            cursor_created_at, cursor_id = decode_cursor(cursor)
        except ValueError as exc:
            from src.core.exceptions import ValidationError

            raise ValidationError(str(exc)) from exc

    rows, next_cursor, unread_count = await notifications_service.list_my_notifications(
        session,
        ctx=ctx,
        limit=limit,
        cursor_created_at=cursor_created_at,
        cursor_id=cursor_id,
        unread_only=unread_only,
    )
    return NotificationListResponse(
        data=[NotificationResponse.model_validate(r) for r in rows],
        next_cursor=next_cursor,
        unread_count=unread_count,
    )


@router.get("/badge", response_model=NotificationBadgeResponse)
async def badge_endpoint(
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> NotificationBadgeResponse:
    """Cheap unread-count endpoint for the navbar bell.

    Hits one COUNT(*) — no list fetch. (When Redis is wired up, this
    should consult `NOTIFICATION_BADGE_CACHE_TTL_SECONDS`.)
    """
    count = (
        await session.execute(
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.recipient_user_id == ctx.user_id,
                Notification.read_at.is_(None),
            )
        )
    ).scalar_one()
    return NotificationBadgeResponse(unread_count=int(count))


@router.get(
    "/preferences",
    response_model=list[NotificationPreferenceResponse],
)
async def list_preferences_endpoint(
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> list[NotificationPreferenceResponse]:
    """List the caller's notification preferences.

    On the first call for a user, lazy-seeds one row per (type, channel)
    with `opted_in = true` so subsequent calls return the stored state.
    """
    if ctx.agency_id is None:
        # SUPER_ADMIN without an agency assignment — no prefs to show.
        return []
    prefs = await list_my_prefs(
        session, user_id=ctx.user_id, agency_id=ctx.agency_id
    )
    return [
        NotificationPreferenceResponse(
            user_id=p.user_id,
            type=p.type,
            channel=p.channel,
            opted_in=p.opted_in,
            updated_at=p.updated_at,
        )
        for p in prefs
    ]


@router.put(
    "/preferences/{type}/{channel}",
    response_model=NotificationPreferenceResponse,
)
async def update_preference_endpoint(
    type: NotificationType,
    channel: NotificationChannel,
    payload: NotificationPreferenceUpdateRequest,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> NotificationPreferenceResponse:
    """Set opt-in/opt-out for one (type, channel) for the caller."""
    if ctx.agency_id is None:
        from src.core.exceptions import ValidationError

        raise ValidationError(
            "Cannot set preferences without an agency assignment."
        )

    row = await set_pref(
        session,
        user_id=ctx.user_id,
        agency_id=ctx.agency_id,
        type=type,
        channel=channel,
        opted_in=payload.opted_in,
    )
    await session.flush()

    # Audit hook — best-effort, never breaks the write.
    ip, ua = request_ip_ua(request)
    try:
        await audit_log(
            session,
            agency_id=ctx.agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.UPDATE,
            entity_type="NOTIFICATION_PREFERENCE",
            entity_id=None,
            new_data={
                "type": type.value,
                "channel": channel.value,
                "opted_in": payload.opted_in,
            },
            ip_address=ip,
            user_agent=ua,
        )
    except Exception as exc:
        log.warning(
            "notifications.prefs_update_audit_failed",
            error=_type(exc).__name__,
        )

    await session.commit()
    return NotificationPreferenceResponse(
        user_id=row.user_id,
        type=row.type,
        channel=row.channel,
        opted_in=row.opted_in,
        updated_at=row.updated_at,
    )


@router.get("/{notification_id}", response_model=NotificationResponse)
async def get_notification_endpoint(
    notification_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> NotificationResponse:
    notif = await notifications_service.get_notification_or_404(
        session, notification_id=notification_id, ctx=ctx
    )
    return NotificationResponse.model_validate(notif)


@router.patch(
    "/{notification_id}/read",
    response_model=NotificationResponse,
)
async def mark_read_endpoint(
    notification_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> NotificationResponse:
    notif = await notifications_service.mark_read(
        session, notification_id=notification_id, ctx=ctx
    )
    await session.commit()
    await session.refresh(notif)
    return NotificationResponse.model_validate(notif)


@router.post(
    "/read-all",
    status_code=status.HTTP_200_OK,
)
async def mark_all_read_endpoint(
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> dict[str, int]:
    count = await notifications_service.mark_all_read(session, ctx=ctx)
    await session.commit()
    log.info(
        "notifications.read_all",
        actor_user_id=str(ctx.user_id),
        marked_count=count,
    )
    return {"marked_count": count}


# --------------------------------------------------------------------------
# Admin endpoints — broadcast
# --------------------------------------------------------------------------
@router.post(
    "/broadcast",
    response_model=BroadcastResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def broadcast_endpoint(
    payload: BroadcastRequest,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    agency_id: Annotated[uuid.UUID | None, Query()] = None,
) -> BroadcastResponse:
    """Fan out one notice to every ACTIVE user in the agency.

    AGENCY_ADMIN: scoped to their own agency (the `?agency_id` query
    param is ignored).

    SUPER_ADMIN: must specify `?agency_id=...` to pick the target.
    """
    if ctx.role != UserRole.SUPER_ADMIN and agency_id is not None:
        # AGENCY_ADMIN can't override their agency. Log + ignore.
        log.info(
            "notifications.broadcast.agency_id_ignored",
            actor=str(ctx.user_id),
            requested=str(agency_id),
        )

    target_agency_id = resolve_broadcast_agency(
        ctx_agency_id=ctx.agency_id,
        ctx_role=ctx.role,
        requested_agency_id=(
            agency_id if ctx.role == UserRole.SUPER_ADMIN else None
        ),
    )

    dispatched, skipped_opted_out, failed = await broadcast_to_agency(
        session,
        agency_id=target_agency_id,
        sender_user_id=ctx.user_id,
        request=payload,
    )

    # Audit hook — one row per broadcast, entity_type="BROADCAST".
    ip, ua = request_ip_ua(request)
    broadcast_id = uuid.uuid4()
    try:
        await audit_log(
            session,
            agency_id=target_agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.CREATE,
            entity_type="BROADCAST",
            entity_id=broadcast_id,
            new_data={
                "type": payload.type.value,
                "title": payload.title,
                "body": payload.body,
                "channel_filter": [c.value for c in payload.channel_filter],
                "metadata": payload.metadata,
                "dispatched": dispatched,
                "skipped_opted_out": skipped_opted_out,
                "failed": failed,
            },
            ip_address=ip,
            user_agent=ua,
        )
    except Exception as exc:
        log.warning(
            "notifications.broadcast_audit_failed",
            error=_type(exc).__name__,
        )

    await session.commit()
    log.info(
        "notifications.broadcast",
        actor=str(ctx.user_id),
        agency_id=str(target_agency_id),
        dispatched=dispatched,
        skipped_opted_out=skipped_opted_out,
        failed=failed,
    )
    return BroadcastResponse(
        dispatched=dispatched,
        skipped_opted_out=skipped_opted_out,
        failed=failed,
    )


__all__ = ["router"]
