"""Appointments service — business logic for appointments + service items.

Routes delegate here. This is the only place that composes ORM operations,
enforces business rules (state-machine validation, patient/staff
existence checks, etc.), and raises the right domain exceptions.

Status lifecycle (see `AppointmentStatus`):

  DRAFT  ─→  SCHEDULED  ─→  AWAITING_CONFIRMATION  ─→  CONFIRMED
                                                          │
                                                          ▼
                                                       ASSIGNED  ─→  CHECKED_IN
                                                                      │
                                                                      ▼
                                                                  IN_PROGRESS
                                                                      │
                                                                      ▼
                                                                  CHECKED_OUT
                                                                      │
                                                                      ▼
                                                                 COMPLETED  ─→
                                                            AWAITING_SERVICE_VERIFICATION
                                                            SERVICE_VERIFIED / DISPUTED
                                                            UNDER_REVIEW
                                                            APPROVED_FOR_BILLING  ─→  PAID

  Branches off the main line:
    - DRAFT / SCHEDULED  → CANCELLED, NO_SHOW, REJECTED
    - AWAITING_CONFIRMATION → CANCELLATION_REQUESTED → CANCELLED
    - CONFIRMED → RESCHEDULE_REQUESTED → SCHEDULED (reschedule cycle)

  Terminal states (no outbound transitions):
    CANCELLED, NO_SHOW, REJECTED, PAID

RLS is the source of truth for tenant scoping; functions still take an
`agency_id` parameter for defence in depth.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.exceptions import (
    ConflictError,
    CrossAgencyAccessDeniedError,
    DuplicateResourceError,
    InvalidStateTransitionError,
    NotFoundError,
    ValidationError,
)
from src.modules.agencies.models import Agency
from src.modules.appointments.events import append_appointment_event
from src.modules.appointments.models import (
    Appointment,
    AppointmentConfirmation,
    AppointmentEvent,
    AppointmentServiceItem,
)
from src.modules.appointments.schemas import (
    AppointmentCancellationRequest,
    AppointmentCancelRequest,
    AppointmentConfirmRequest,
    AppointmentCreateRequest,
    AppointmentRescheduleRequest,
    AppointmentServiceItemCreateRequest,
    AppointmentServiceItemUpdateRequest,
    AppointmentStatusTransitionRequest,
    AppointmentUpdateRequest,
)
from src.modules.patients.models import (
    GuardianProfile,
    PatientGuardianRelationship,
    PatientProfile,
)
from src.modules.staff.models import StaffProfile
from src.shared.domain.enums import (
    AppointmentEventType,
    AppointmentStatus,
    ConfirmationStatus,
    ServiceItemStatus,
    UserRole,
)
from src.shared.utils.datetime_utils import utc_now

# --------------------------------------------------------------------------
# State machine
# --------------------------------------------------------------------------
# Allowed forward transitions. The keys are the FROM state, values are the
# set of TO states that are valid. Terminal states (CANCELLED, NO_SHOW,
# REJECTED, PAID) have empty sets.
_ALLOWED_TRANSITIONS: dict[AppointmentStatus, frozenset[AppointmentStatus]] = {
    AppointmentStatus.DRAFT: frozenset(
        {
            AppointmentStatus.SCHEDULED,
            AppointmentStatus.CANCELLED,
        }
    ),
    AppointmentStatus.SCHEDULED: frozenset(
        {
            AppointmentStatus.NOTIFICATION_SENT,
            AppointmentStatus.AWAITING_CONFIRMATION,
            AppointmentStatus.CANCELLED,
            AppointmentStatus.NO_SHOW,
            AppointmentStatus.REJECTED,
        }
    ),
    AppointmentStatus.NOTIFICATION_SENT: frozenset(
        {
            AppointmentStatus.AWAITING_CONFIRMATION,
            AppointmentStatus.CANCELLED,
            AppointmentStatus.NO_SHOW,
        }
    ),
    AppointmentStatus.AWAITING_CONFIRMATION: frozenset(
        {
            AppointmentStatus.CONFIRMED,
            AppointmentStatus.CANCELLATION_REQUESTED,
            AppointmentStatus.RESCHEDULE_REQUESTED,
            AppointmentStatus.NO_SHOW,
            AppointmentStatus.REJECTED,
        }
    ),
    AppointmentStatus.CONFIRMED: frozenset(
        {
            AppointmentStatus.RESCHEDULE_REQUESTED,
            AppointmentStatus.CANCELLATION_REQUESTED,
            AppointmentStatus.ASSIGNED,
            AppointmentStatus.CANCELLED,
            AppointmentStatus.NO_SHOW,
        }
    ),
    AppointmentStatus.RESCHEDULE_REQUESTED: frozenset(
        {
            AppointmentStatus.SCHEDULED,
            AppointmentStatus.CANCELLED,
        }
    ),
    AppointmentStatus.CANCELLATION_REQUESTED: frozenset(
        {
            AppointmentStatus.CANCELLED,
            AppointmentStatus.CONFIRMED,  # patient changed their mind
        }
    ),
    AppointmentStatus.ASSIGNED: frozenset(
        {
            AppointmentStatus.CHECKED_IN,
            AppointmentStatus.CANCELLED,
            AppointmentStatus.NO_SHOW,
        }
    ),
    AppointmentStatus.CHECKED_IN: frozenset(
        {
            AppointmentStatus.IN_PROGRESS,
            AppointmentStatus.CHECKED_OUT,
            AppointmentStatus.NO_SHOW,
        }
    ),
    AppointmentStatus.IN_PROGRESS: frozenset(
        {
            AppointmentStatus.CHECKED_OUT,
        }
    ),
    AppointmentStatus.CHECKED_OUT: frozenset(
        {
            AppointmentStatus.COMPLETED,
        }
    ),
    AppointmentStatus.COMPLETED: frozenset(
        {
            AppointmentStatus.AWAITING_SERVICE_VERIFICATION,
            AppointmentStatus.DISPUTED,
        }
    ),
    AppointmentStatus.AWAITING_SERVICE_VERIFICATION: frozenset(
        {
            AppointmentStatus.SERVICE_VERIFIED,
            AppointmentStatus.DISPUTED,
        }
    ),
    AppointmentStatus.SERVICE_VERIFIED: frozenset(
        {
            AppointmentStatus.UNDER_REVIEW,
            AppointmentStatus.APPROVED_FOR_BILLING,
            AppointmentStatus.DISPUTED,
        }
    ),
    AppointmentStatus.DISPUTED: frozenset(
        {
            AppointmentStatus.UNDER_REVIEW,
            AppointmentStatus.SERVICE_VERIFIED,
        }
    ),
    AppointmentStatus.UNDER_REVIEW: frozenset(
        {
            AppointmentStatus.APPROVED_FOR_BILLING,
            AppointmentStatus.DISPUTED,
            AppointmentStatus.SERVICE_VERIFIED,
        }
    ),
    AppointmentStatus.APPROVED_FOR_BILLING: frozenset(
        {
            AppointmentStatus.PAID,
        }
    ),
    # Terminal — no outbound transitions
    AppointmentStatus.CANCELLED: frozenset(),
    AppointmentStatus.NO_SHOW: frozenset(),
    AppointmentStatus.REJECTED: frozenset(),
    AppointmentStatus.PAID: frozenset(),
}


def _is_transition_allowed(
    from_state: AppointmentStatus, to_state: AppointmentStatus
) -> bool:
    """Return True iff the (from → to) edge exists in the state machine."""
    return to_state in _ALLOWED_TRANSITIONS.get(from_state, frozenset())


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
async def _assert_agency_active(session: AsyncSession, agency_id: uuid.UUID) -> None:
    """Cheap sanity check — don't schedule against a churned agency."""
    agency = await session.get(Agency, agency_id)
    if agency is None:
        raise NotFoundError(details={"resource": "agency", "id": str(agency_id)})
    if agency.status.value == "CHURNED":
        raise ConflictError(
            "Cannot modify appointments on a churned agency.",
            details={"agency_id": str(agency_id), "status": agency.status.value},
        )


async def _get_appointment_or_404(
    session: AsyncSession,
    *,
    appointment_id: uuid.UUID,
    agency_id: uuid.UUID,
    with_items: bool = False,
    with_patient: bool = False,
) -> Appointment:
    stmt = select(Appointment).where(
        Appointment.id == appointment_id,
        Appointment.agency_id == agency_id,
    )
    if with_items:
        stmt = stmt.options(selectinload(Appointment.service_items))
    if with_patient:
        stmt = stmt.options(selectinload(Appointment.patient))
    appt = (await session.execute(stmt)).scalar_one_or_none()
    if appt is None:
        raise NotFoundError(
            details={"resource": "appointment", "id": str(appointment_id)}
        )
    return appt


async def _get_service_item_or_404(
    session: AsyncSession,
    *,
    item_id: uuid.UUID,
    appointment_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> AppointmentServiceItem:
    stmt = select(AppointmentServiceItem).where(
        AppointmentServiceItem.id == item_id,
        AppointmentServiceItem.appointment_id == appointment_id,
        AppointmentServiceItem.agency_id == agency_id,
    )
    item = (await session.execute(stmt)).scalar_one_or_none()
    if item is None:
        raise NotFoundError(
            details={
                "resource": "appointment_service_item",
                "id": str(item_id),
            }
        )
    return item


async def _assert_patient_exists(
    session: AsyncSession,
    *,
    patient_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> None:
    stmt = select(PatientProfile.id).where(
        PatientProfile.id == patient_id, PatientProfile.agency_id == agency_id
    )
    if (await session.execute(stmt)).scalar_one_or_none() is None:
        raise NotFoundError(details={"resource": "patient_profile", "id": str(patient_id)})


async def _assert_staff_exists(
    session: AsyncSession,
    *,
    staff_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> None:
    stmt = select(StaffProfile.id).where(
        StaffProfile.id == staff_id, StaffProfile.agency_id == agency_id
    )
    if (await session.execute(stmt)).scalar_one_or_none() is None:
        raise NotFoundError(details={"resource": "staff_profile", "id": str(staff_id)})


async def _assert_actor_may_act_for_patient(
    session: AsyncSession,
    *,
    actor_user_id: uuid.UUID,
    actor_role: UserRole,
    patient_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> None:
    """Authorise an actor to act on behalf of a patient.

    Rules:
      - AGENCY_ADMIN at the agency: always allowed.
      - PATIENT: must own the appointment (patient.user_id == actor).
      - GUARDIAN: must have an active legal guardian relationship to
        the patient at the agency.
      - STAFF / SUPER_ADMIN: not allowed on patient-initiated actions.
    Raises:
        CrossAgencyAccessDeniedError: actor has no relationship to the
            patient at the agency.
    """
    if actor_role == UserRole.AGENCY_ADMIN:
        return

    if actor_role == UserRole.PATIENT:
        owner = (
            await session.execute(
                select(PatientProfile.user_id).where(
                    PatientProfile.id == patient_id,
                    PatientProfile.agency_id == agency_id,
                )
            )
        ).scalar_one_or_none()
        if owner != actor_user_id:
            # Privacy: don't leak the existence of an appointment to a
            # patient who doesn't own it. 404 is the right answer.
            raise NotFoundError(
                details={"resource": "appointment", "id": str(patient_id)}
            )
        return

    if actor_role == UserRole.GUARDIAN:
        # Resolve the guardian profile for this user at the agency.
        guardian = (
            await session.execute(
                select(GuardianProfile.id).where(
                    GuardianProfile.user_id == actor_user_id,
                    GuardianProfile.agency_id == agency_id,
                )
            )
        ).scalar_one_or_none()
        if guardian is None:
            # Privacy: don't leak the existence of an appointment to a
            # guardian who has no link at this agency.
            raise NotFoundError(
                details={"resource": "appointment", "id": str(patient_id)}
            )
        # Look up an active (non-expired) legal relationship.
        today = date.today()
        rel = (
            await session.execute(
                select(PatientGuardianRelationship.id).where(
                    PatientGuardianRelationship.guardian_id == guardian,
                    PatientGuardianRelationship.patient_id == patient_id,
                    PatientGuardianRelationship.agency_id == agency_id,
                    PatientGuardianRelationship.is_legal.is_(True),
                )
            )
        ).scalar_one_or_none()
        if rel is None:
            raise NotFoundError(
                details={"resource": "appointment", "id": str(patient_id)}
            )
        # Check the optional validity window.
        validity = (
            await session.execute(
                select(
                    PatientGuardianRelationship.valid_from,
                    PatientGuardianRelationship.valid_until,
                ).where(PatientGuardianRelationship.id == rel)
            )
        ).one_or_none()
        if validity is not None:
            vfrom, vuntil = validity
            if vfrom is not None and vfrom > today:
                raise NotFoundError(
                    details={"resource": "appointment", "id": str(patient_id)}
                )
            if vuntil is not None and vuntil < today:
                raise NotFoundError(
                    details={"resource": "appointment", "id": str(patient_id)}
                )
        return

    raise CrossAgencyAccessDeniedError(
        details={"reason": f"role {actor_role.value} may not act on patient behalf"}
    )


def _extract_constraint(exc: IntegrityError) -> str:
    """Pull a constraint name out of a Postgres IntegrityError, if possible."""
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    if diag is not None and getattr(diag, "constraint_name", None):
        return diag.constraint_name
    return "unknown"


# --------------------------------------------------------------------------
# Appointments — CRUD
# --------------------------------------------------------------------------
async def create_appointment(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    payload: AppointmentCreateRequest,
    scheduled_by_user_id: uuid.UUID,
) -> Appointment:
    """Schedule a new appointment at the caller's agency.

    Validates that the patient (required) and staff (optional) exist at
    the same agency, then creates the appointment + any inline service
    items in a single transaction.

    The new row starts in `DRAFT` by default. Callers transition state
    via `transition_status` (e.g. DRAFT → SCHEDULED → AWAITING_CONFIRMATION).
    """
    await _assert_agency_active(session, agency_id)
    await _assert_patient_exists(
        session, patient_id=payload.patient_id, agency_id=agency_id
    )
    if payload.staff_id is not None:
        await _assert_staff_exists(
            session, staff_id=payload.staff_id, agency_id=agency_id
        )

    appt = Appointment(
        agency_id=agency_id,
        patient_id=payload.patient_id,
        staff_id=payload.staff_id,
        program_type=payload.program_type,
        scheduled_start=payload.scheduled_start,
        scheduled_end=payload.scheduled_end,
        location=payload.location,
        notes=payload.notes,
        status=AppointmentStatus.DRAFT,
    )
    session.add(appt)
    try:
        await session.flush()  # populate appt.id
    except IntegrityError as exc:
        await session.rollback()
        raise DuplicateResourceError(
            "Could not create appointment (constraint violation).",
            details={"constraint": _extract_constraint(exc)},
        ) from exc

    # Inline service items (if any)
    for item_payload in payload.service_items:
        item = AppointmentServiceItem(
            appointment_id=appt.id,
            agency_id=agency_id,
            service_type=item_payload.service_type,
            planned_minutes=item_payload.planned_minutes,
            notes=item_payload.notes,
        )
        session.add(item)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ValidationError(
            "Service item violates a check constraint.",
            details={"constraint": _extract_constraint(exc)},
        ) from exc

    # Refresh to populate relationships for response serialization
    await session.refresh(appt, attribute_names=["service_items"])
    return appt


async def get_appointment(
    session: AsyncSession,
    *,
    appointment_id: uuid.UUID,
    agency_id: uuid.UUID,
    with_items: bool = False,
    with_patient: bool = False,
) -> Appointment:
    """Fetch a single appointment, optionally with nested service items
    and/or the linked PatientProfile eager-loaded."""
    return await _get_appointment_or_404(
        session,
        appointment_id=appointment_id,
        agency_id=agency_id,
        with_items=with_items,
        with_patient=with_patient,
    )


async def list_appointments(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    patient_id: uuid.UUID | None = None,
    staff_id: uuid.UUID | None = None,
    status_filter: AppointmentStatus | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[Sequence[Appointment], int]:
    """Paginated list of appointments at the caller's agency.

    Optional filters narrow by patient, staff, and/or status. Sorted by
    `scheduled_start DESC` (newest first) so the calendar view can page
    backwards through history.
    """
    page = max(1, page)
    page_size = max(1, min(100, page_size))

    base = select(Appointment).where(Appointment.agency_id == agency_id)
    count_base = (
        select(func.count())
        .select_from(Appointment)
        .where(Appointment.agency_id == agency_id)
    )
    if patient_id is not None:
        base = base.where(Appointment.patient_id == patient_id)
        count_base = count_base.where(Appointment.patient_id == patient_id)
    if staff_id is not None:
        base = base.where(Appointment.staff_id == staff_id)
        count_base = count_base.where(Appointment.staff_id == staff_id)
    if status_filter is not None:
        base = base.where(Appointment.status == status_filter)
        count_base = count_base.where(Appointment.status == status_filter)

    base = (
        base.order_by(Appointment.scheduled_start.desc(), Appointment.id)
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    rows = (await session.execute(base)).scalars().all()
    total = (await session.execute(count_base)).scalar_one()
    return rows, int(total)


async def update_appointment(
    session: AsyncSession,
    *,
    appointment_id: uuid.UUID,
    agency_id: uuid.UUID,
    payload: AppointmentUpdateRequest,
) -> Appointment:
    """Patch an appointment. Omitted fields are unchanged.

    Status transitions are NOT done here — use `transition_status` or
    `cancel_appointment` instead. This keeps the state machine in one
    place.
    """
    appt = await _get_appointment_or_404(
        session, appointment_id=appointment_id, agency_id=agency_id
    )

    # Block edits once the visit is in flight or terminal.
    if appt.status in {
        AppointmentStatus.CHECKED_IN,
        AppointmentStatus.IN_PROGRESS,
        AppointmentStatus.CHECKED_OUT,
        AppointmentStatus.COMPLETED,
        AppointmentStatus.AWAITING_SERVICE_VERIFICATION,
        AppointmentStatus.SERVICE_VERIFIED,
        AppointmentStatus.UNDER_REVIEW,
        AppointmentStatus.APPROVED_FOR_BILLING,
        AppointmentStatus.PAID,
        AppointmentStatus.CANCELLED,
        AppointmentStatus.NO_SHOW,
        AppointmentStatus.REJECTED,
    }:
        raise InvalidStateTransitionError(
            "Cannot edit an appointment in its current state.",
            details={
                "current_status": appt.status.value,
                "blocked_actions": "edit window / staff / program",
            },
        )

    if payload.staff_id is not None:
        if payload.staff_id != appt.staff_id:
            await _assert_staff_exists(
                session, staff_id=payload.staff_id, agency_id=agency_id
            )
        appt.staff_id = payload.staff_id
    if payload.program_type is not None:
        appt.program_type = payload.program_type
    if payload.scheduled_start is not None:
        appt.scheduled_start = payload.scheduled_start
    if payload.scheduled_end is not None:
        appt.scheduled_end = payload.scheduled_end
    if payload.location is not None:
        appt.location = payload.location
    if payload.notes is not None:
        appt.notes = payload.notes

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ValidationError(
            "Appointment update violates a check constraint.",
            details={"constraint": _extract_constraint(exc)},
        ) from exc
    return appt


# --------------------------------------------------------------------------
# Status transitions
# --------------------------------------------------------------------------
async def transition_status(
    session: AsyncSession,
    *,
    appointment_id: uuid.UUID,
    agency_id: uuid.UUID,
    payload: AppointmentStatusTransitionRequest,
    actor_user_id: uuid.UUID | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> Appointment:
    """Move the appointment through the lifecycle state machine.

    Validates the (current → requested) edge exists; otherwise raises
    `InvalidStateTransitionError`. Also stamps the lifecycle timestamps
    (confirmed_at, checked_in_at, etc.) when applicable. Appends one
    `STATUS_TRANSITION` event on success.
    """
    appt = await _get_appointment_or_404(
        session, appointment_id=appointment_id, agency_id=agency_id
    )

    if appt.status == payload.status:
        # No-op — return the appointment unchanged.
        return appt

    if not _is_transition_allowed(appt.status, payload.status):
        raise InvalidStateTransitionError(
            f"Cannot transition from {appt.status.value} to {payload.status.value}.",
            details={"from": appt.status.value, "to": payload.status.value},
        )

    # Some transitions require a staff assignee (e.g. CHECKED_IN is meaningless
    # without a staff member). Only ASSIGNED status requires it; CHECKED_IN
    # also implies a staff member performed the check-in.
    if payload.status in {
        AppointmentStatus.ASSIGNED,
        AppointmentStatus.CHECKED_IN,
        AppointmentStatus.IN_PROGRESS,
    } and appt.staff_id is None:
        raise ConflictError(
            "Cannot transition to this status without an assigned staff member.",
            details={"current_status": appt.status.value, "requested_status": payload.status.value},
        )

    # Confirmation flow side-effects
    if payload.confirmation_status is not None:
        appt.confirmation_status = payload.confirmation_status
        if payload.confirmation_status == ConfirmationStatus.CONFIRMED:
            appt.confirmed_at = utc_now()
        if payload.note:
            appt.confirmation_note = payload.note

    # Visit timestamp side-effects
    if payload.status == AppointmentStatus.CHECKED_IN and appt.checked_in_at is None:
        appt.checked_in_at = utc_now()
    if payload.status == AppointmentStatus.CHECKED_OUT and appt.checked_out_at is None:
        appt.checked_out_at = utc_now()
    if payload.status == AppointmentStatus.COMPLETED and appt.completed_at is None:
        appt.completed_at = utc_now()

    from_status = appt.status
    appt.status = payload.status
    await session.flush()

    await append_appointment_event(
        session,
        agency_id=agency_id,
        appointment_id=appt.id,
        event_type=AppointmentEventType.STATUS_TRANSITION,
        actor_user_id=actor_user_id,
        from_status=from_status,
        to_status=appt.status,
        metadata={"note": payload.note},
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return appt


async def cancel_appointment(
    session: AsyncSession,
    *,
    appointment_id: uuid.UUID,
    agency_id: uuid.UUID,
    payload: AppointmentCancelRequest,
    actor_user_id: uuid.UUID | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> Appointment:
    """Cancel an appointment.

    Cancellation is only allowed BEFORE the visit is checked in. After
    that, use `transition_status` to mark NO_SHOW or to walk the dispute
    flow. Appends one `CANCELLED_BY_ADMIN` event on success.
    """
    appt = await _get_appointment_or_404(
        session, appointment_id=appointment_id, agency_id=agency_id
    )

    if appt.status == AppointmentStatus.CANCELLED:
        # Idempotent — return as-is (no event)
        return appt

    if appt.status in {
        AppointmentStatus.CHECKED_IN,
        AppointmentStatus.IN_PROGRESS,
        AppointmentStatus.CHECKED_OUT,
        AppointmentStatus.COMPLETED,
        AppointmentStatus.AWAITING_SERVICE_VERIFICATION,
        AppointmentStatus.SERVICE_VERIFIED,
        AppointmentStatus.UNDER_REVIEW,
        AppointmentStatus.APPROVED_FOR_BILLING,
        AppointmentStatus.PAID,
    }:
        raise InvalidStateTransitionError(
            "Cannot cancel an appointment that is already in progress or completed.",
            details={"current_status": appt.status.value},
        )

    from_status = appt.status
    appt.status = AppointmentStatus.CANCELLED
    appt.cancelled_reason = payload.reason
    appt.cancelled_at = utc_now()
    await session.flush()

    await append_appointment_event(
        session,
        agency_id=agency_id,
        appointment_id=appt.id,
        event_type=AppointmentEventType.CANCELLED_BY_ADMIN,
        actor_user_id=actor_user_id,
        from_status=from_status,
        to_status=appt.status,
        metadata={"reason": payload.reason},
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return appt


async def assign_staff(
    session: AsyncSession,
    *,
    appointment_id: uuid.UUID,
    agency_id: uuid.UUID,
    staff_id: uuid.UUID,
) -> Appointment:
    """Assign (or re-assign) the staff member who will perform the visit.

    Allowed in pre-visit states only. After CHECKED_IN, the staff is
    locked in (callers should use the dispute flow to re-assign later).
    """
    appt = await _get_appointment_or_404(
        session, appointment_id=appointment_id, agency_id=agency_id
    )

    if appt.status in {
        AppointmentStatus.CHECKED_IN,
        AppointmentStatus.IN_PROGRESS,
        AppointmentStatus.CHECKED_OUT,
        AppointmentStatus.COMPLETED,
        AppointmentStatus.AWAITING_SERVICE_VERIFICATION,
        AppointmentStatus.SERVICE_VERIFIED,
        AppointmentStatus.UNDER_REVIEW,
        AppointmentStatus.APPROVED_FOR_BILLING,
        AppointmentStatus.PAID,
        AppointmentStatus.CANCELLED,
        AppointmentStatus.NO_SHOW,
        AppointmentStatus.REJECTED,
    }:
        raise InvalidStateTransitionError(
            "Cannot change staff assignment in the current state.",
            details={"current_status": appt.status.value},
        )

    await _assert_staff_exists(session, staff_id=staff_id, agency_id=agency_id)
    appt.staff_id = staff_id
    await session.flush()
    return appt


# --------------------------------------------------------------------------
# Service items
# --------------------------------------------------------------------------
async def list_service_items(
    session: AsyncSession,
    *,
    appointment_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> Sequence[AppointmentServiceItem]:
    """List service items for an appointment."""
    await _get_appointment_or_404(
        session, appointment_id=appointment_id, agency_id=agency_id
    )
    stmt = (
        select(AppointmentServiceItem)
        .where(
            AppointmentServiceItem.appointment_id == appointment_id,
            AppointmentServiceItem.agency_id == agency_id,
        )
        .order_by(AppointmentServiceItem.created_at.asc())
    )
    return (await session.execute(stmt)).scalars().all()


async def add_service_item(
    session: AsyncSession,
    *,
    appointment_id: uuid.UUID,
    agency_id: uuid.UUID,
    payload: AppointmentServiceItemCreateRequest,
) -> AppointmentServiceItem:
    """Add a service item to an existing appointment.

    Once the visit is COMPLETED, no more service items can be added —
    use the dispute flow instead.
    """
    appt = await _get_appointment_or_404(
        session, appointment_id=appointment_id, agency_id=agency_id
    )
    if appt.status in {
        AppointmentStatus.COMPLETED,
        AppointmentStatus.AWAITING_SERVICE_VERIFICATION,
        AppointmentStatus.SERVICE_VERIFIED,
        AppointmentStatus.UNDER_REVIEW,
        AppointmentStatus.APPROVED_FOR_BILLING,
        AppointmentStatus.PAID,
        AppointmentStatus.CANCELLED,
        AppointmentStatus.NO_SHOW,
        AppointmentStatus.REJECTED,
    }:
        raise InvalidStateTransitionError(
            "Cannot add service items to a finalized appointment.",
            details={"current_status": appt.status.value},
        )

    item = AppointmentServiceItem(
        appointment_id=appt.id,
        agency_id=agency_id,
        service_type=payload.service_type,
        planned_minutes=payload.planned_minutes,
        notes=payload.notes,
    )
    session.add(item)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ValidationError(
            "Service item violates a check constraint.",
            details={"constraint": _extract_constraint(exc)},
        ) from exc
    return item


async def update_service_item(
    session: AsyncSession,
    *,
    item_id: uuid.UUID,
    appointment_id: uuid.UUID,
    agency_id: uuid.UUID,
    payload: AppointmentServiceItemUpdateRequest,
) -> AppointmentServiceItem:
    """Patch a service item (status, notes, etc.)."""
    item = await _get_service_item_or_404(
        session,
        item_id=item_id,
        appointment_id=appointment_id,
        agency_id=agency_id,
    )

    if payload.service_type is not None:
        item.service_type = payload.service_type
    if payload.planned_minutes is not None:
        item.planned_minutes = payload.planned_minutes
    if payload.notes is not None:
        item.notes = payload.notes
    if payload.status is not None and payload.status != item.status:
        # Don't allow rewinding a DONE item back to PENDING — that would
        # hide billing-relevant state.
        if item.status == ServiceItemStatus.DONE and payload.status != ServiceItemStatus.DONE:
            raise InvalidStateTransitionError(
                "Cannot move a DONE service item back to a non-final status.",
                details={"from": item.status.value, "to": payload.status.value},
            )
        item.status = payload.status

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ValidationError(
            "Service item update violates a check constraint.",
            details={"constraint": _extract_constraint(exc)},
        ) from exc
    return item


async def delete_service_item(
    session: AsyncSession,
    *,
    item_id: uuid.UUID,
    appointment_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> None:
    """Remove a service item.

    Only allowed for PENDING items — once an item has been delivered
    (DONE / NOT_DONE / etc.) it's part of the clinical record.
    """
    item = await _get_service_item_or_404(
        session,
        item_id=item_id,
        appointment_id=appointment_id,
        agency_id=agency_id,
    )
    if item.status != ServiceItemStatus.PENDING:
        raise InvalidStateTransitionError(
            "Cannot delete a service item that has been delivered.",
            details={"current_status": item.status.value},
        )
    await session.delete(item)
    await session.flush()


# --------------------------------------------------------------------------
# Lifecycle — patient-facing confirm / reschedule / cancel-request
# --------------------------------------------------------------------------
async def confirm_appointment(
    session: AsyncSession,
    *,
    appointment_id: uuid.UUID,
    agency_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    actor_role: UserRole,
    payload: AppointmentConfirmRequest,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> tuple[Appointment, AppointmentConfirmation]:
    """Patient/guardian/admin confirms or declines an appointment.

    Allowed from `AWAITING_CONFIRMATION`, `SCHEDULED`, `NOTIFICATION_SENT`
    (admin override). Writes a row to `appointment_confirmations` (1:1
    upsert — re-confirming overwrites the prior row) and appends an
    `AppointmentEvent`. Best-effort fan-out to staff notifications.
    """
    appt = await _get_appointment_or_404(
        session, appointment_id=appointment_id, agency_id=agency_id
    )

    # Role gate.
    if actor_role not in {UserRole.PATIENT, UserRole.GUARDIAN, UserRole.AGENCY_ADMIN}:
        raise CrossAgencyAccessDeniedError(
            details={"reason": "only PATIENT/GUARDIAN/AGENCY_ADMIN may confirm"}
        )

    # Ownership/relationship check (AGENCY_ADMIN passes through).
    await _assert_actor_may_act_for_patient(
        session,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        patient_id=appt.patient_id,
        agency_id=agency_id,
    )

    # State-machine gate.
    allowed_from = {
        AppointmentStatus.AWAITING_CONFIRMATION,
        AppointmentStatus.SCHEDULED,
        AppointmentStatus.NOTIFICATION_SENT,
    }
    if appt.status not in allowed_from:
        raise InvalidStateTransitionError(
            "Cannot confirm an appointment in its current state.",
            details={"current_status": appt.status.value},
        )

    from_status = appt.status
    new_confirmation_status = (
        ConfirmationStatus.DECLINED if payload.declined else ConfirmationStatus.CONFIRMED
    )

    # Upsert the confirmation row (1:1 with the appointment).
    confirmation_role = (
        UserRole.GUARDIAN if actor_role == UserRole.GUARDIAN else UserRole.PATIENT
    )

    existing = (
        await session.execute(
            select(AppointmentConfirmation).where(
                AppointmentConfirmation.appointment_id == appt.id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.confirmed_by = actor_user_id
        existing.confirmation_role = confirmation_role
        existing.status = new_confirmation_status
        existing.comment = payload.comment
        existing.created_at = utc_now()
        confirmation = existing
    else:
        confirmation = AppointmentConfirmation(
            appointment_id=appt.id,
            confirmed_by=actor_user_id,
            confirmation_role=confirmation_role,
            status=new_confirmation_status,
            comment=payload.comment,
        )
        session.add(confirmation)
        await session.flush()

    # Move the appointment forward only on CONFIRMED; DECLINED leaves
    # the status as-is (admin still has to /cancel to finalise).
    if not payload.declined:
        if not _is_transition_allowed(appt.status, AppointmentStatus.CONFIRMED):
            raise InvalidStateTransitionError(
                f"Cannot transition from {appt.status.value} to CONFIRMED.",
                details={"from": appt.status.value},
            )
        appt.status = AppointmentStatus.CONFIRMED
        appt.confirmation_status = ConfirmationStatus.CONFIRMED
        appt.confirmed_at = utc_now()
        if payload.comment:
            appt.confirmation_note = payload.comment

    to_status = appt.status

    await append_appointment_event(
        session,
        agency_id=agency_id,
        appointment_id=appt.id,
        event_type=AppointmentEventType.CONFIRMATION_FILED,
        actor_user_id=actor_user_id,
        from_status=from_status,
        to_status=to_status,
        metadata={"declined": payload.declined, "comment": payload.comment},
        ip_address=ip_address,
        user_agent=user_agent,
    )

    # Best-effort staff notification (only on confirmed, not declined).
    if not payload.declined:
        try:
            from src.modules.notifications.integrations import (
                notify_appointment_confirmed,
            )

            await notify_appointment_confirmed(
                session, appointment_id=appt.id, agency_id=agency_id
            )
        except Exception:
            # Swallow — failures here must never break the write.
            pass

    await session.flush()
    return appt, confirmation


async def request_reschedule(
    session: AsyncSession,
    *,
    appointment_id: uuid.UUID,
    agency_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    actor_role: UserRole,
    payload: AppointmentRescheduleRequest,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> Appointment:
    """Patient/guardian/admin proposes a new visit window.

    The appointment moves to `RESCHEDULE_REQUESTED`; the admin reviews
    and either patches the existing `scheduled_start`/`scheduled_end`
    (via PATCH) or cancels. The proposed window is stored in the event's
    `metadata` so it's visible in the timeline.
    """
    appt = await _get_appointment_or_404(
        session, appointment_id=appointment_id, agency_id=agency_id
    )

    if actor_role not in {UserRole.PATIENT, UserRole.GUARDIAN, UserRole.AGENCY_ADMIN}:
        raise CrossAgencyAccessDeniedError(
            details={"reason": "only PATIENT/GUARDIAN/AGENCY_ADMIN may request"}
        )

    await _assert_actor_may_act_for_patient(
        session,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        patient_id=appt.patient_id,
        agency_id=agency_id,
    )

    allowed_from = {
        AppointmentStatus.AWAITING_CONFIRMATION,
        AppointmentStatus.SCHEDULED,
        AppointmentStatus.NOTIFICATION_SENT,
        AppointmentStatus.CONFIRMED,
    }
    if appt.status not in allowed_from:
        raise InvalidStateTransitionError(
            "Cannot request a reschedule in the current state.",
            details={"current_status": appt.status.value},
        )
    if not _is_transition_allowed(appt.status, AppointmentStatus.RESCHEDULE_REQUESTED):
        raise InvalidStateTransitionError(
            f"Cannot transition from {appt.status.value} to RESCHEDULE_REQUESTED.",
            details={"from": appt.status.value},
        )

    from_status = appt.status
    appt.status = AppointmentStatus.RESCHEDULE_REQUESTED
    to_status = appt.status

    await append_appointment_event(
        session,
        agency_id=agency_id,
        appointment_id=appt.id,
        event_type=AppointmentEventType.RESCHEDULE_REQUESTED,
        actor_user_id=actor_user_id,
        from_status=from_status,
        to_status=to_status,
        metadata={
            "proposed_start": payload.proposed_start.isoformat(),
            "proposed_end": payload.proposed_end.isoformat(),
            "comment": payload.comment,
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )

    try:
        from src.modules.notifications.integrations import (
            notify_appointment_reschedule_requested,
        )

        await notify_appointment_reschedule_requested(
            session,
            appointment_id=appt.id,
            agency_id=agency_id,
            proposed_start=payload.proposed_start,
            proposed_end=payload.proposed_end,
        )
    except Exception:
        pass

    await session.flush()
    return appt


async def request_cancellation(
    session: AsyncSession,
    *,
    appointment_id: uuid.UUID,
    agency_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    actor_role: UserRole,
    payload: AppointmentCancellationRequest,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> Appointment:
    """Patient/guardian/admin requests cancellation. Moves the appointment
    to `CANCELLATION_REQUESTED`; admin finalises via the existing /cancel."""
    appt = await _get_appointment_or_404(
        session, appointment_id=appointment_id, agency_id=agency_id
    )

    if actor_role not in {UserRole.PATIENT, UserRole.GUARDIAN, UserRole.AGENCY_ADMIN}:
        raise CrossAgencyAccessDeniedError(
            details={"reason": "only PATIENT/GUARDIAN/AGENCY_ADMIN may request"}
        )

    await _assert_actor_may_act_for_patient(
        session,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        patient_id=appt.patient_id,
        agency_id=agency_id,
    )

    allowed_from = {
        AppointmentStatus.AWAITING_CONFIRMATION,
        AppointmentStatus.CONFIRMED,
        AppointmentStatus.SCHEDULED,
        AppointmentStatus.NOTIFICATION_SENT,
        AppointmentStatus.RESCHEDULE_REQUESTED,
    }
    if appt.status not in allowed_from:
        raise InvalidStateTransitionError(
            "Cannot request cancellation in the current state.",
            details={"current_status": appt.status.value},
        )
    if not _is_transition_allowed(
        appt.status, AppointmentStatus.CANCELLATION_REQUESTED
    ):
        raise InvalidStateTransitionError(
            f"Cannot transition from {appt.status.value} to CANCELLATION_REQUESTED.",
            details={"from": appt.status.value},
        )

    from_status = appt.status
    appt.status = AppointmentStatus.CANCELLATION_REQUESTED
    # Stash the reason so admin reviewers see it on the row.
    appt.cancelled_reason = payload.reason
    to_status = appt.status

    await append_appointment_event(
        session,
        agency_id=agency_id,
        appointment_id=appt.id,
        event_type=AppointmentEventType.CANCELLATION_REQUESTED,
        actor_user_id=actor_user_id,
        from_status=from_status,
        to_status=to_status,
        metadata={"reason": payload.reason},
        ip_address=ip_address,
        user_agent=user_agent,
    )

    try:
        from src.modules.notifications.integrations import (
            notify_appointment_cancellation_requested,
        )

        await notify_appointment_cancellation_requested(
            session,
            appointment_id=appt.id,
            agency_id=agency_id,
            reason=payload.reason,
        )
    except Exception:
        pass

    await session.flush()
    return appt


async def list_appointment_events(
    session: AsyncSession,
    *,
    appointment_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> list[AppointmentEvent]:
    """List events for one appointment, oldest first (timeline order)."""
    await _get_appointment_or_404(
        session, appointment_id=appointment_id, agency_id=agency_id
    )
    stmt = (
        select(AppointmentEvent)
        .where(
            AppointmentEvent.appointment_id == appointment_id,
            AppointmentEvent.agency_id == agency_id,
        )
        .order_by(AppointmentEvent.created_at.asc(), AppointmentEvent.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_latest_confirmation(
    session: AsyncSession,
    *,
    appointment_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> AppointmentConfirmation | None:
    """Return the (single) confirmation row for an appointment, or None."""
    await _get_appointment_or_404(
        session, appointment_id=appointment_id, agency_id=agency_id
    )
    return (
        await session.execute(
            select(AppointmentConfirmation).where(
                AppointmentConfirmation.appointment_id == appointment_id
            )
        )
    ).scalar_one_or_none()


__all__ = [
    "add_service_item",
    "assign_staff",
    "cancel_appointment",
    "confirm_appointment",
    "create_appointment",
    "delete_service_item",
    "get_appointment",
    "get_latest_confirmation",
    "list_appointment_events",
    "list_appointments",
    "list_service_items",
    "request_cancellation",
    "request_reschedule",
    "transition_status",
    "update_appointment",
    "update_service_item",
]
