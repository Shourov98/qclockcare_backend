"""Notification fan-out helpers — called by other modules after their writes
commit. Each helper resolves the relevant recipient user_ids and dispatches
notifications through `notifications_service.dispatch_notification`.

The functions are best-effort: they catch + log exceptions so a notification
failure can never break the underlying write. Use `BackgroundTasks` from
the router if you want true out-of-band dispatch (Phase 2).
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.logging import get_logger
from src.modules.notifications import service as notifications_service
from src.modules.patients.models import (
    PatientGuardianRelationship,
    PatientProfile,
)
from src.modules.staff.models import StaffProfile
from src.modules.visits.models import Visit
from src.shared.domain.enums import NotificationType

log = get_logger(__name__)


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


async def notify_visit_checked_in(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> None:
    """Fan out a VISIT_CHECKED_IN notification to the patient + their guardians.

    Best-effort — exceptions are logged and swallowed.
    """
    try:
        user_ids = await _recipient_ids_for_visit_patient(
            session, visit_id=visit_id, agency_id=agency_id
        )
        for uid in user_ids:
            await notifications_service.dispatch_notification(
                session,
                agency_id=agency_id,
                recipient_user_id=uid,
                type=NotificationType.VISIT_CHECKED_IN,
                title="Your visit has started",
                body="Your care professional has checked in.",
                metadata={
                    "entity_id": str(visit_id),
                    "visit_id": str(visit_id),
                },
            )
    except Exception as exc:
        log.warning(
            "notifications.notify_visit_checked_in_failed",
            visit_id=str(visit_id),
            error=type(exc).__name__,
            detail=str(exc),
        )


async def notify_visit_checked_out(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> None:
    """Fan out VISIT_CHECKED_OUT so the patient can review + verify."""
    try:
        user_ids = await _recipient_ids_for_visit_patient(
            session, visit_id=visit_id, agency_id=agency_id
        )
        for uid in user_ids:
            await notifications_service.dispatch_notification(
                session,
                agency_id=agency_id,
                recipient_user_id=uid,
                type=NotificationType.VISIT_CHECKED_OUT,
                title="Your visit has ended",
                body="Please review the services and confirm or report any issues.",
                metadata={
                    "entity_id": str(visit_id),
                    "visit_id": str(visit_id),
                },
            )
    except Exception as exc:
        log.warning(
            "notifications.notify_visit_checked_out_failed",
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


async def notify_verification_status(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
    verified: bool,
) -> None:
    """Notify the assigned staff when a verification is filed.

    `verified=True` → SERVICE_VERIFIED. `verified=False` → SERVICE_DISPUTED.
    """
    staff_user_id = await _staff_user_id_for_visit(session, visit_id=visit_id)
    if staff_user_id is None:
        return
    try:
        await notifications_service.dispatch_notification(
            session,
            agency_id=agency_id,
            recipient_user_id=staff_user_id,
            type=(
                NotificationType.SERVICE_VERIFIED
                if verified
                else NotificationType.SERVICE_DISPUTED
            ),
            title=(
                "Services verified" if verified else "Services disputed"
            ),
            body=(
                "The patient/guardian has confirmed the services."
                if verified
                else "The patient/guardian has disputed the services. Please follow up."
            ),
            metadata={"entity_id": str(visit_id), "visit_id": str(visit_id)},
        )
    except Exception as exc:
        log.warning(
            "notifications.notify_verification_status_failed",
            visit_id=str(visit_id),
            error=type(exc).__name__,
            detail=str(exc),
        )


async def notify_visit_issue_filed(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
    issue_type: str,
) -> None:
    """Notify the assigned staff when an issue is filed against their visit."""
    staff_user_id = await _staff_user_id_for_visit(session, visit_id=visit_id)
    if staff_user_id is None:
        return
    try:
        await notifications_service.dispatch_notification(
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
    except Exception as exc:
        log.warning(
            "notifications.notify_visit_issue_filed_failed",
            visit_id=str(visit_id),
            error=type(exc).__name__,
            detail=str(exc),
        )


__all__ = [
    "notify_verification_status",
    "notify_visit_checked_in",
    "notify_visit_checked_out",
    "notify_visit_issue_filed",
]
