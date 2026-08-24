"""Notification fan-out helpers — called by other modules after their writes
commit. Each helper resolves the relevant recipient user_ids and dispatches
notifications through `notifications_service.dispatch_notification`.

The functions are best-effort: they catch + log exceptions so a notification
failure can never break the underlying write.

Phase 1 split (this module):
  - The synchronous half (`service.dispatch_notification` →
    `deliveries.prepare_deliveries`) runs inline in the request thread.
    It inserts the in-app `Notification` row + one PENDING
    `NotificationDelivery` row per available channel, so the in-app
    surface is durable before the HTTP response returns.
  - The network-call half (`deliveries.dispatch_provider_phase`,
    via `background.run_dispatch_in_background`) is scheduled on
    FastAPI's `BackgroundTasks` so an unreachable SMTP server cannot
    hang the request for tens of seconds.
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import BackgroundTasks
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.logging import get_logger
from src.modules.notifications import service as notifications_service
from src.modules.notifications.background import run_dispatch_in_background
from src.modules.patients.models import (
    PatientGuardianRelationship,
    PatientProfile,
)
from src.modules.staff.models import StaffProfile
from src.modules.visits.models import Visit
from src.shared.domain.enums import NotificationType, UserRole

log = get_logger(__name__)


def _schedule_dispatch(
    background_tasks: BackgroundTasks,
    *,
    actor_user_id: uuid.UUID,
    actor_agency_id: uuid.UUID,
    actor_role: UserRole,
    notification_id: uuid.UUID,
    deliveries: list[tuple[object, uuid.UUID]],
) -> None:
    """Schedule the provider-call phase on FastAPI's BackgroundTasks.

    The actual `provider.send(...)` calls run after the HTTP response
    is sent, so an unreachable SMTP server cannot block the request
    thread. See `background.run_dispatch_in_background`.
    """
    background_tasks.add_task(
        run_dispatch_in_background,
        actor_user_id=actor_user_id,
        actor_agency_id=actor_agency_id,
        actor_role=actor_role,
        notification_id=notification_id,
        deliveries=deliveries,
    )


async def _recipient_ids_for_visit_patient(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> list[uuid.UUID]:
    """Return user_ids that should receive patient-targeted visit notifications.

    Includes:
    - The patient themselves (if they have a user_id)
    - Each active legal guardian (is_legal=true AND valid_until NULL or >= today)
    """
    visit = (
        await session.execute(select(Visit).where(Visit.id == visit_id))
    ).scalar_one_or_none()
    if visit is None:
        return []
    patient = (
        await session.execute(
            select(PatientProfile).where(
                PatientProfile.id == visit.appointment.patient_id
            )
        )
    ).scalar_one_or_none()
    if patient is None:
        return []

    recipients: set[uuid.UUID] = set()
    if patient.user_id is not None:
        recipients.add(patient.user_id)

    today = date.today()
    rels = (
        await session.execute(
            select(PatientGuardianRelationship).where(
                PatientGuardianRelationship.patient_id == patient.id,
                PatientGuardianRelationship.agency_id == agency_id,
                PatientGuardianRelationship.is_legal.is_(True),
            )
        )
    ).scalars().all()
    from src.modules.patients.models import GuardianProfile

    for rel in rels:
        if rel.valid_until is not None and rel.valid_until < today:
            continue
        guardian = (
            await session.execute(
                select(GuardianProfile).where(
                    GuardianProfile.id == rel.guardian_id
                )
            )
        ).scalar_one_or_none()
        if guardian is not None and guardian.user_id is not None:
            recipients.add(guardian.user_id)

    return list(recipients)


async def notify_visit_started(
    background_tasks: BackgroundTasks,
    session: AsyncSession,
    *,
    actor_user_id: uuid.UUID,
    actor_agency_id: uuid.UUID,
    actor_role: UserRole,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> None:
    """Fan out a VISIT_STARTED notification to the patient + their guardians.

    The in-app Notification row + per-channel PENDING delivery rows
    are inserted synchronously; the provider network calls
    (EMAIL/SMS) are deferred to FastAPI's BackgroundTasks so an
    unreachable SMTP server cannot block the request thread.

    Best-effort — exceptions are logged and swallowed.
    """
    try:
        user_ids = await _recipient_ids_for_visit_patient(
            session, visit_id=visit_id, agency_id=agency_id
        )
        for uid in user_ids:
            result = await notifications_service.dispatch_notification(
                session,
                agency_id=agency_id,
                recipient_user_id=uid,
                type=NotificationType.VISIT_STARTED,
                title="Your visit has started",
                body="Your care professional has checked in.",
                metadata={
                    "entity_id": str(visit_id),
                    "visit_id": str(visit_id),
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
            "notifications.notify_visit_started_failed",
            visit_id=str(visit_id),
            error=type(exc).__name__,
            detail=str(exc),
        )


async def notify_visit_ended(
    background_tasks: BackgroundTasks,
    session: AsyncSession,
    *,
    actor_user_id: uuid.UUID,
    actor_agency_id: uuid.UUID,
    actor_role: UserRole,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> None:
    """Fan out VISIT_ENDED so the patient knows the caregiver has left."""
    try:
        user_ids = await _recipient_ids_for_visit_patient(
            session, visit_id=visit_id, agency_id=agency_id
        )
        for uid in user_ids:
            result = await notifications_service.dispatch_notification(
                session,
                agency_id=agency_id,
                recipient_user_id=uid,
                type=NotificationType.VISIT_ENDED,
                title="Your visit has ended",
                body="Your care professional has finished today's visit.",
                metadata={
                    "entity_id": str(visit_id),
                    "visit_id": str(visit_id),
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
            "notifications.notify_visit_ended_failed",
            visit_id=str(visit_id),
            error=type(exc).__name__,
            detail=str(exc),
        )


async def _staff_user_id_for_visit(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
) -> uuid.UUID | None:
    """Return the user_id of the staff assigned to this visit, or None."""
    visit = (
        await session.execute(select(Visit).where(Visit.id == visit_id))
    ).scalar_one_or_none()
    if visit is None:
        return None
    staff = (
        await session.execute(
            select(StaffProfile).where(StaffProfile.id == visit.staff_id)
        )
    ).scalar_one_or_none()
    return staff.user_id if staff else None


async def notify_visit_signed(
    background_tasks: BackgroundTasks,
    session: AsyncSession,
    *,
    actor_user_id: uuid.UUID,
    actor_agency_id: uuid.UUID,
    actor_role: UserRole,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> None:
    """Notify the assigned staff that the patient/guardian has signed the visit."""
    staff_user_id = await _staff_user_id_for_visit(session, visit_id=visit_id)
    if staff_user_id is None:
        return
    try:
        result = await notifications_service.dispatch_notification(
            session,
            agency_id=agency_id,
            recipient_user_id=staff_user_id,
            type=NotificationType.VISIT_SIGNED,
            title="Visit signed",
            body="The patient/guardian has signed off on the visit.",
            metadata={"entity_id": str(visit_id), "visit_id": str(visit_id)},
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
            "notifications.notify_visit_signed_failed",
            visit_id=str(visit_id),
            error=type(exc).__name__,
            detail=str(exc),
        )


async def notify_visit_issue_filed(
    background_tasks: BackgroundTasks,
    session: AsyncSession,
    *,
    actor_user_id: uuid.UUID,
    actor_agency_id: uuid.UUID,
    actor_role: UserRole,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
    issue_type: str,
) -> None:
    """Notify the assigned staff when an issue is filed against their visit."""
    staff_user_id = await _staff_user_id_for_visit(session, visit_id=visit_id)
    if staff_user_id is None:
        return
    try:
        result = await notifications_service.dispatch_notification(
            session,
            agency_id=agency_id,
            recipient_user_id=staff_user_id,
            type=NotificationType.GENERIC,  # no dedicated ISSUE_FILED enum value
            title=f"New issue filed: {issue_type}",
            body="An issue was reported against a visit you worked on.",
            metadata={
                "entity_id": str(visit_id),
                "visit_id": str(visit_id),
                "issue_type": issue_type,
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
            "notifications.notify_visit_issue_filed_failed",
            visit_id=str(visit_id),
            error=type(exc).__name__,
            detail=str(exc),
        )


# --------------------------------------------------------------------------
# Appointment lifecycle helpers
# --------------------------------------------------------------------------
async def _staff_user_id_for_appointment(
    session: AsyncSession,
    *,
    appointment_id: uuid.UUID,
) -> uuid.UUID | None:
    """Return the user_id of the staff assigned to this appointment, if any."""
    from src.modules.appointments.models import Appointment

    appt = (
        await session.execute(
            select(Appointment).where(Appointment.id == appointment_id)
        )
    ).scalar_one_or_none()
    if appt is None or appt.staff_id is None:
        return None
    staff = (
        await session.execute(
            select(StaffProfile).where(StaffProfile.id == appt.staff_id)
        )
    ).scalar_one_or_none()
    return staff.user_id if staff else None


async def _agency_admin_user_ids(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
) -> list[uuid.UUID]:
    """Return the user_ids of all AGENCY_ADMINs at an agency.

    Used by lifecycle helpers to route review requests to humans.
    """
    from src.modules.identity.models import UserRoleAssignment

    rows = (
        await session.execute(
            select(UserRoleAssignment.user_id).where(
                UserRoleAssignment.agency_id == agency_id,
                UserRoleAssignment.role == UserRole.AGENCY_ADMIN,
            )
        )
    ).scalars().all()
    return list(rows)


async def notify_appointment_marked_ready(
    background_tasks: BackgroundTasks,
    session: AsyncSession,
    *,
    actor_user_id: uuid.UUID,
    actor_agency_id: uuid.UUID,
    actor_role: UserRole,
    appointment_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> None:
    """Notify the assigned staff that an appointment is READY for the visit.

    Triggered when the admin flips `SCHEDULED → READY`. The caregiver
    is expected to call `POST /visits` next to actually start the visit
    (which transitions `READY → IN_PROGRESS`).
    """
    try:
        staff_user_id = await _staff_user_id_for_appointment(
            session, appointment_id=appointment_id
        )
        if staff_user_id is None:
            return
        result = await notifications_service.dispatch_notification(
            session,
            agency_id=agency_id,
            recipient_user_id=staff_user_id,
            type=NotificationType.APPOINTMENT_READY,
            title="Appointment ready",
            body="A scheduled visit is ready to start.",
            metadata={"entity_id": str(appointment_id), "appointment_id": str(appointment_id)},
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
            "notifications.notify_appointment_marked_ready_failed",
            appointment_id=str(appointment_id),
            error=type(exc).__name__,
            detail=str(exc),
        )


__all__ = [
    "notify_appointment_marked_ready",
    "notify_visit_ended",
    "notify_visit_issue_filed",
    "notify_visit_signed",
    "notify_visit_started",
]
