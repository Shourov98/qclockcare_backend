"""Background-task dispatch for notification provider calls.

When a write endpoint fires a notification, the synchronous path
(`service.dispatch_notification` + `deliveries.prepare_deliveries`)
inserts the `Notification` row + per-channel PENDING
`NotificationDelivery` rows so the in-app surface is durable before
the HTTP response returns. The actual provider network calls (SMTP,
Twilio, …) are deferred to FastAPI's `BackgroundTasks` so that an
unreachable SMTP server cannot block the request thread for tens of
seconds.

`run_dispatch_in_background(...)` is the entry point scheduled by
`BackgroundTasks.add_task(...)` from the integration helpers
(`notify_visit_*`, `notify_appointment_*`, etc.). It:

  1. Opens a fresh `AsyncSession` via `session_scope()` — no JWT in
     the background, so `get_session_with_auth` is not appropriate.
  2. Calls `set_session_context` with the actor's
     `(user_id, agency_id, role)` captured at schedule time. This
     re-establishes the RLS GUCs that the request thread had set,
     so the UPDATE on `notifications` + `notification_deliveries`
     passes the RLS policies.
  3. Reloads the `Notification` row by id (it must already be
     committed from the request thread).
  4. Calls `dispatch_provider_phase` to invoke each provider and
     UPDATE the delivery rows.

The function catches all exceptions broadly and logs at error level.
A background-task crash must NEVER take down the worker; the delivery
rows simply stay at PENDING and a future Phase-2 retry layer can pick
them up.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import session_scope, set_session_context
from src.core.logging import get_logger
from src.modules.notifications.deliveries import dispatch_provider_phase
from src.modules.notifications.models import Notification
from src.shared.domain.enums import NotificationChannel, UserRole

log = get_logger(__name__)


async def run_dispatch_in_background(
    *,
    actor_user_id: uuid.UUID,
    actor_agency_id: uuid.UUID,
    actor_role: UserRole | str,
    notification_id: uuid.UUID,
    deliveries: list[tuple[NotificationChannel | str, uuid.UUID]],
) -> None:
    """Run the provider-call phase of a notification dispatch off-thread.

    Intended to be scheduled via `BackgroundTasks.add_task(...)` from
    the integration helpers. Never raises — exceptions are logged.
    """
    # Normalise the channel values into NotificationChannel instances in
    # case the caller passed strings (the integration helpers store the
    # raw enum, but be defensive for future callers).
    normalised: list[tuple[NotificationChannel, uuid.UUID]] = []
    for ch, did in deliveries:
        if isinstance(ch, NotificationChannel):
            normalised.append((ch, did))
        else:
            try:
                normalised.append((NotificationChannel(ch), did))
            except ValueError:
                log.error(
                    "notifications.background.unknown_channel",
                    notification_id=str(notification_id),
                    channel=str(ch),
                )
    deliveries = normalised

    role_value = actor_role.value if isinstance(actor_role, UserRole) else str(actor_role)

    try:
        async with session_scope() as session:
            await _run_in_background_inner(
                session,
                actor_user_id=actor_user_id,
                actor_agency_id=actor_agency_id,
                actor_role=role_value,
                notification_id=notification_id,
                deliveries=deliveries,
            )
    except Exception as exc:
        log.error(
            "notifications.background.run_failed",
            notification_id=str(notification_id),
            error=type(exc).__name__,
            detail=str(exc),
        )


async def _run_in_background_inner(
    session: AsyncSession,
    *,
    actor_user_id: uuid.UUID,
    actor_agency_id: uuid.UUID,
    actor_role: str,
    notification_id: uuid.UUID,
    deliveries: list[tuple[NotificationChannel, uuid.UUID]],
) -> None:
    """Inner body of the background task. Runs inside `session_scope`."""
    # 1. Re-establish the actor's RLS context so the UPDATE on
    #    `notifications` + `notification_deliveries` passes the
    #    policies. Without this, `current_setting('app.current_user_id')`
    #    would be empty and the UPDATE would be rejected.
    await set_session_context(
        session,
        user_id=str(actor_user_id),
        agency_id=str(actor_agency_id),
        user_role=actor_role,
    )

    # 2. Reload the notification row.
    notification = (
        await session.execute(
            select(Notification).where(Notification.id == notification_id)
        )
    ).scalar_one_or_none()
    if notification is None:
        log.error(
            "notifications.background.notification_missing",
            notification_id=str(notification_id),
        )
        return

    # 3. Run the provider phase.
    await dispatch_provider_phase(
        session, notification=notification, deliveries=deliveries
    )


__all__ = ["run_dispatch_in_background"]
