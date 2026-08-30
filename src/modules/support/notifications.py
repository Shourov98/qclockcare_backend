"""Notification fan-out for the support-ticket module.

Two helpers:
  - `notify_agency_admins_of_new_ticket` — called when a patient /
    guardian opens a ticket; fans a `SUPPORT_TICKET_OPENED` notification
    out to every AGENCY_ADMIN at the agency so the inbox lights up.
  - `notify_reporter_of_admin_reply` — called when an AGENCY_ADMIN
    replies on a ticket; fans `SUPPORT_TICKET_REPLIED` back to the
    ticket's reporter (patient or guardian).

Both reuse the existing `notifications_service.dispatch_notification`
+ `notifications/integrations._schedule_dispatch` pipeline so the
in-app bell + EMAIL/SMS channels are wired without re-inventing the
fan-out logic.

Errors are caught + logged (best-effort) so a notification failure
never breaks the underlying ticket write — the user's message has
already been saved at that point.
"""

from __future__ import annotations

import uuid

from fastapi import BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.logging import get_logger
from src.modules.notifications import service as notifications_service
from src.modules.notifications.integrations import (
    _agency_admin_user_ids,
    _schedule_dispatch,
)
from src.shared.domain.enums import NotificationType, UserRole

log = get_logger(__name__)


_PREVIEW_LIMIT = 200


def _preview(body: str | None) -> str:
    if not body:
        return ""
    body = body.strip().replace("\n", " ")
    if len(body) <= _PREVIEW_LIMIT:
        return body
    return body[: _PREVIEW_LIMIT - 1].rstrip() + "…"


async def notify_agency_admins_of_new_ticket(
    background_tasks: BackgroundTasks,
    session: AsyncSession,
    *,
    actor_user_id: uuid.UUID,
    actor_agency_id: uuid.UUID,
    actor_role: UserRole,
    agency_id: uuid.UUID,
    ticket_id: uuid.UUID,
    subject: str,
    preview: str,
) -> None:
    """Fan a SUPPORT_TICKET_OPENED notification to all AGENCY_ADMINs."""
    try:
        admin_ids = await _agency_admin_user_ids(
            session, agency_id=agency_id
        )
        for uid in admin_ids:
            result = await notifications_service.dispatch_notification(
                session,
                agency_id=agency_id,
                recipient_user_id=uid,
                type=NotificationType.SUPPORT_TICKET_OPENED,
                title=f"New support ticket: {subject}",
                body=_preview(preview),
                metadata={
                    "entity_id": str(ticket_id),
                    "ticket_id": str(ticket_id),
                },
            )
            if result is not None:
                notification, deliveries = result
                _schedule_dispatch(
                    background_tasks,
                    actor_user_id=actor_user_id,
                    actor_agency_id=actor_agency_id,
                    actor_role=actor_role,
                    notification_id=notification.id,
                    deliveries=deliveries,
                )
    except Exception as exc:
        log.warning(
            "support.notify_admins_failed",
            ticket_id=str(ticket_id),
            error=type(exc).__name__,
            detail=str(exc),
        )


async def notify_reporter_of_admin_reply(
    background_tasks: BackgroundTasks,
    session: AsyncSession,
    *,
    actor_user_id: uuid.UUID,
    actor_agency_id: uuid.UUID,
    actor_role: UserRole,
    agency_id: uuid.UUID,
    ticket_id: uuid.UUID,
    recipient_user_id: uuid.UUID,
    subject: str,
    preview: str,
) -> None:
    """Fan a SUPPORT_TICKET_REPLIED notification back to the reporter."""
    try:
        result = await notifications_service.dispatch_notification(
            session,
            agency_id=agency_id,
            recipient_user_id=recipient_user_id,
            type=NotificationType.SUPPORT_TICKET_REPLIED,
            title=f"Update on your support ticket: {subject}",
            body=_preview(preview),
            metadata={
                "entity_id": str(ticket_id),
                "ticket_id": str(ticket_id),
            },
        )
        if result is not None:
            notification, deliveries = result
            _schedule_dispatch(
                background_tasks,
                actor_user_id=actor_user_id,
                actor_agency_id=actor_agency_id,
                actor_role=actor_role,
                notification_id=notification.id,
                deliveries=deliveries,
            )
    except Exception as exc:
        log.warning(
            "support.notify_reporter_failed",
            ticket_id=str(ticket_id),
            recipient_user_id=str(recipient_user_id),
            error=type(exc).__name__,
            detail=str(exc),
        )


__all__ = [
    "notify_agency_admins_of_new_ticket",
    "notify_reporter_of_admin_reply",
]