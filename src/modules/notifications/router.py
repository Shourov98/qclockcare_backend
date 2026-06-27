"""Notifications router — `/notifications` recipient endpoints.

Endpoints:
  GET    /notifications                  — list caller's notifications (cursor paginated)
  GET    /notifications/{id}             — single notification
  PATCH  /notifications/{id}/read        — mark one as read
  POST   /notifications/read-all         — mark all as read (returns count)

No INSERT/DELETE endpoints. New rows are written via
`notifications_service.dispatch_notification` called from other modules.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.logging import get_logger
from src.modules.identity.dependencies import (
    CurrentAuth,
    get_session_with_auth,
)
from src.modules.notifications import service as notifications_service
from src.modules.notifications.schemas import (
    NotificationListResponse,
    NotificationResponse,
)
from src.shared.schemas.pagination import decode_cursor

router = APIRouter(prefix="/notifications", tags=["notifications"])
log = get_logger(__name__)


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


__all__ = ["router"]
