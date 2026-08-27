"""Appointments service — business logic for appointments + activities.

Routes delegate here. This is the only place that composes ORM operations,
enforces business rules (state-machine validation, patient/staff
existence checks, etc.), and raises the right domain exceptions.

5-state lifecycle per the spec (see `AppointmentStatus`):

  SCHEDULED → READY → IN_PROGRESS → AWAITING_SIGNATURE → COMPLETED
              ↘  CANCELLED / MISSED / REJECTED  ↙

The legacy confirmation / reschedule / cancel-request flows are gone —
the spec's `AppointmentSignature` replaces them. EVV start/end records
and signatures live on the `visits` module.

RLS is the source of truth for tenant scoping; functions still take an
`agency_id` parameter for defence in depth.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta

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
from src.modules.appointments.models import Appointment, AppointmentActivity
from src.modules.appointments.schemas import (
    AppointmentActivityCreateRequest,
    AppointmentActivityUpdateRequest,
    AppointmentCancelRequest,
    AppointmentCreateRequest,
    AppointmentMissedRequest,
    AppointmentRejectedRequest,
    AppointmentStatusTransitionRequest,
    AppointmentUpdateRequest,
)
from src.modules.patients.models import (
    GuardianProfile,
    PatientGuardianRelationship,
    PatientProfile,
)
from src.modules.staff.models import StaffProfile, StaffQualification
from src.shared.domain.enums import (
    AppointmentStatus,
    ServiceItemStatus,
    UserRole,
)
from src.shared.utils.datetime_utils import utc_now

# --------------------------------------------------------------------------
# State machine — 5-state lifecycle per the spec
# --------------------------------------------------------------------------
# Allowed forward transitions. The keys are the FROM state, values are
# the set of TO states that are valid. Terminal states (COMPLETED,
# CANCELLED, MISSED, REJECTED) have empty sets.
#
#   SCHEDULED → READY → IN_PROGRESS → AWAITING_SIGNATURE → COMPLETED
#               ↘  CANCELLED / MISSED / REJECTED  ↙
_ALLOWED_TRANSITIONS: dict[AppointmentStatus, frozenset[AppointmentStatus]] = {
    AppointmentStatus.SCHEDULED: frozenset(
        {
            AppointmentStatus.READY,
            AppointmentStatus.CANCELLED,
            AppointmentStatus.MISSED,
            AppointmentStatus.REJECTED,
        }
    ),
    AppointmentStatus.READY: frozenset(
        {
            AppointmentStatus.IN_PROGRESS,
            AppointmentStatus.CANCELLED,
            AppointmentStatus.MISSED,
        }
    ),
    AppointmentStatus.IN_PROGRESS: frozenset(
        {
            AppointmentStatus.AWAITING_SIGNATURE,
            AppointmentStatus.CANCELLED,
            AppointmentStatus.MISSED,
        }
    ),
    AppointmentStatus.AWAITING_SIGNATURE: frozenset(
        {
            AppointmentStatus.COMPLETED,
            AppointmentStatus.CANCELLED,
        }
    ),
    # Terminal — no outbound transitions
    AppointmentStatus.COMPLETED: frozenset(),
    AppointmentStatus.CANCELLED: frozenset(),
    AppointmentStatus.MISSED: frozenset(),
    AppointmentStatus.REJECTED: frozenset(),
}


def _is_transition_allowed(
    from_state: AppointmentStatus, to_state: AppointmentStatus
) -> bool:
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
    with_activities: bool = False,
    with_patient: bool = False,
) -> Appointment:
    stmt = select(Appointment).where(
        Appointment.id == appointment_id,
        Appointment.agency_id == agency_id,
    )
    if with_activities:
        stmt = stmt.options(selectinload(Appointment.activities))
    if with_patient:
        stmt = stmt.options(selectinload(Appointment.patient))
    appt = (await session.execute(stmt)).scalar_one_or_none()
    if appt is None:
        raise NotFoundError(
            details={"resource": "appointment", "id": str(appointment_id)}
        )
    return appt


async def _get_activity_or_404(
    session: AsyncSession,
    *,
    activity_id: uuid.UUID,
    appointment_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> AppointmentActivity:
    stmt = select(AppointmentActivity).where(
        AppointmentActivity.id == activity_id,
        AppointmentActivity.appointment_id == appointment_id,
        AppointmentActivity.agency_id == agency_id,
    )
    a = (await session.execute(stmt)).scalar_one_or_none()
    if a is None:
        raise NotFoundError(
            details={"resource": "appointment_activity", "id": str(activity_id)}
        )
    return a


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
            raise NotFoundError(
                details={"resource": "appointment", "id": str(patient_id)}
            )
        return

    if actor_role == UserRole.GUARDIAN:
        guardian = (
            await session.execute(
                select(GuardianProfile.id).where(
                    GuardianProfile.user_id == actor_user_id,
                    GuardianProfile.agency_id == agency_id,
                )
            )
        ).scalar_one_or_none()
        if guardian is None:
            raise NotFoundError(
                details={"resource": "appointment", "id": str(patient_id)}
            )
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
        return

    raise CrossAgencyAccessDeniedError(
        details={"reason": f"role {actor_role.value} may not act on patient behalf"}
    )


def _extract_constraint(exc: IntegrityError) -> str:
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

    New rows start in `SCHEDULED`. The admin marks them `READY` via
    `mark_appointment_ready` once the caregiver is notified; the
    caregiver then calls `POST /visits` to start the visit (transition
    to `IN_PROGRESS`).
    """
    # Unused parameter; kept for forward-compat (audit log plumbing).
    _ = scheduled_by_user_id
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
        status=AppointmentStatus.SCHEDULED,
        billing_status="unpaid",
    )
    session.add(appt)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise DuplicateResourceError(
            "Could not create appointment (constraint violation).",
            details={"constraint": _extract_constraint(exc)},
        ) from exc

    # Generate the externally-rendered `claim_id`. Format:
    # `CG-{agency_name_short}-{appt_id_short}` — e.g. `CG-QLOC-A1B2C3D4`.
    # The `claim_id` is created here (not via DB default) so the agency
    # tag is sourced from the freshly-fetched agency row (the router
    # doesn't have to pass it through). The Agency table doesn't have a
    # dedicated `code` column so we render the first 4 uppercase
    # alphanumeric characters of `agency.name`.
    agency = await session.get(Agency, agency_id)
    if agency is not None and getattr(agency, "name", None):
        # Strip non-alphanumeric and uppercase the first 4 chars.
        tag = "".join(
            ch for ch in agency.name.upper() if ch.isalnum()
        )[:4] or "AGCY"
    else:
        tag = "AGCY"
    appt.claim_id = f"CG-{tag}-{str(appt.id)[:8].upper()}"
    await session.flush()

    # Inline activities (if any)
    for activity_payload in payload.activities:
        activity = AppointmentActivity(
            appointment_id=appt.id,
            agency_id=agency_id,
            name=activity_payload.name,
            planned_minutes=activity_payload.planned_minutes,
            notes=activity_payload.notes,
        )
        session.add(activity)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ValidationError(
            "Activity violates a check constraint.",
            details={"constraint": _extract_constraint(exc)},
        ) from exc

    await session.refresh(appt, attribute_names=["activities"])
    return appt


async def get_appointment(
    session: AsyncSession,
    *,
    appointment_id: uuid.UUID,
    agency_id: uuid.UUID,
    with_activities: bool = False,
    with_patient: bool = False,
) -> Appointment:
    return await _get_appointment_or_404(
        session,
        appointment_id=appointment_id,
        agency_id=agency_id,
        with_activities=with_activities,
        with_patient=with_patient,
    )


async def list_appointments(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    patient_id: uuid.UUID | None = None,
    staff_id: uuid.UUID | None = None,
    status_filter: AppointmentStatus | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[Sequence[Appointment], int]:
    """Paginated list of appointments at the caller's agency.

    Eager-loads the caregiver (`staff.user`), staff profile (with first
    qualification), patient identity, and parent activities so the
    patient mobile app can render a fully populated card in a single
    round trip.

    Date filtering
    --------------
    `date_from` / `date_to` are *calendar dates* in the caller's locale
    (the FE sends `YYYY-MM-DD`). They filter on `scheduled_start`:
      - `date_from` only — `scheduled_start >= date_from 00:00:00`
      - `date_to` only   — `scheduled_start < (date_to + 1 day)`
      - both              — the inclusive window
      - neither           — no date filter
    The semantics are inclusive on both ends ("appointments scheduled
    on Tuesday" passes both `date_from=date_to=Tuesday`).
    """
    page = max(1, page)
    page_size = max(1, min(100, page_size))

    base = (
        select(Appointment)
        .where(Appointment.agency_id == agency_id)
        .options(
            selectinload(Appointment.staff).selectinload(StaffProfile.user),
            selectinload(Appointment.staff).selectinload(
                StaffProfile.qualifications
            ),
            selectinload(Appointment.patient).selectinload(
                PatientProfile.user
            ),
            selectinload(Appointment.activities),
        )
    )
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
    if date_from is not None:
        dt_from = datetime.combine(date_from, time.min)
        base = base.where(Appointment.scheduled_start >= dt_from)
        count_base = count_base.where(Appointment.scheduled_start >= dt_from)
    if date_to is not None:
        # Add one day so `date_to` is inclusive of the entire day.
        dt_to_excl = datetime.combine(date_to + timedelta(days=1), time.min)
        base = base.where(Appointment.scheduled_start < dt_to_excl)
        count_base = count_base.where(Appointment.scheduled_start < dt_to_excl)

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
        AppointmentStatus.IN_PROGRESS,
        AppointmentStatus.AWAITING_SIGNATURE,
        AppointmentStatus.COMPLETED,
        AppointmentStatus.CANCELLED,
        AppointmentStatus.MISSED,
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
) -> Appointment:
    """Move the appointment through the lifecycle state machine.

    Validates the (current → requested) edge exists; otherwise raises
    `InvalidStateTransitionError`. Some transitions require an assigned
    staff member (e.g. `READY → IN_PROGRESS` only happens via
    `POST /visits`, which the visits module enforces; this generic
    endpoint only walks the state machine for admin overrides).
    """
    appt = await _get_appointment_or_404(
        session, appointment_id=appointment_id, agency_id=agency_id
    )

    if appt.status == payload.status:
        return appt

    if not _is_transition_allowed(appt.status, payload.status):
        raise InvalidStateTransitionError(
            f"Cannot transition from {appt.status.value} to {payload.status.value}.",
            details={"from": appt.status.value, "to": payload.status.value},
        )

    # `READY → IN_PROGRESS` requires the staff assignment to be locked
    # in. `IN_PROGRESS → AWAITING_SIGNATURE` and beyond are driven by
    # the visits module (gated by activities + billing + signature).
    if (
        payload.status in {AppointmentStatus.READY, AppointmentStatus.IN_PROGRESS}
        and appt.staff_id is None
    ):
        raise ConflictError(
            "Cannot transition to this status without an assigned staff member.",
            details={
                "current_status": appt.status.value,
                "requested_status": payload.status.value,
            },
        )

    appt.status = payload.status
    # Unused argument; kept so the signature matches the audit_logs
    # plumbing in the router.
    _ = actor_user_id
    await session.flush()
    return appt


async def mark_appointment_ready(
    session: AsyncSession,
    *,
    appointment_id: uuid.UUID,
    agency_id: uuid.UUID,
    actor_user_id: uuid.UUID | None = None,
) -> Appointment:
    """Convenience helper — admin marks an appointment as READY.

    Same effect as `transition_status(... READY)`. Kept as a named
    helper so the router's `POST /appointments/{id}/ready` endpoint
    reads cleanly.
    """
    appt = await _get_appointment_or_404(
        session, appointment_id=appointment_id, agency_id=agency_id
    )
    if appt.status != AppointmentStatus.SCHEDULED:
        raise InvalidStateTransitionError(
            "Only SCHEDULED appointments can be marked ready.",
            details={"current_status": appt.status.value},
        )
    if appt.staff_id is None:
        raise ConflictError(
            "Cannot mark an appointment ready without an assigned staff member.",
            details={"appointment_id": str(appt.id)},
        )
    appt.status = AppointmentStatus.READY
    _ = actor_user_id
    await session.flush()
    return appt


async def cancel_appointment(
    session: AsyncSession,
    *,
    appointment_id: uuid.UUID,
    agency_id: uuid.UUID,
    payload: AppointmentCancelRequest,
    actor_user_id: uuid.UUID | None = None,
) -> Appointment:
    """Cancel an appointment.

    Allowed only BEFORE the visit is in progress. After `IN_PROGRESS`,
    the visit-side transition should be used (or `MISSED`).
    """
    appt = await _get_appointment_or_404(
        session, appointment_id=appointment_id, agency_id=agency_id
    )

    if appt.status == AppointmentStatus.CANCELLED:
        return appt

    if appt.status in {
        AppointmentStatus.IN_PROGRESS,
        AppointmentStatus.AWAITING_SIGNATURE,
        AppointmentStatus.COMPLETED,
    }:
        raise InvalidStateTransitionError(
            "Cannot cancel an appointment that is already in progress or completed.",
            details={"current_status": appt.status.value},
        )

    appt.status = AppointmentStatus.CANCELLED
    appt.cancelled_reason = payload.reason
    appt.cancelled_at = utc_now()
    _ = actor_user_id
    await session.flush()
    return appt


async def mark_appointment_missed(
    session: AsyncSession,
    *,
    appointment_id: uuid.UUID,
    agency_id: uuid.UUID,
    payload: AppointmentMissedRequest,
    actor_user_id: uuid.UUID | None = None,
) -> Appointment:
    """Mark an appointment as MISSED (caregiver no-show / patient unavailable).

    Allowed from `SCHEDULED` or `READY`. If a visit has been started
    (`IN_PROGRESS`), the visit-side transition should be used instead —
    marking the *appointment* as MISSED here would orphan the in-flight
    `Visit` row.
    """
    appt = await _get_appointment_or_404(
        session, appointment_id=appointment_id, agency_id=agency_id
    )

    if appt.status == AppointmentStatus.MISSED:
        return appt  # idempotent

    if appt.status not in {
        AppointmentStatus.SCHEDULED,
        AppointmentStatus.READY,
    }:
        raise InvalidStateTransitionError(
            "Only SCHEDULED or READY appointments can be marked MISSED.",
            details={
                "current_status": appt.status.value,
                "requested_status": AppointmentStatus.MISSED.value,
            },
        )

    appt.status = AppointmentStatus.MISSED
    appt.cancelled_reason = payload.reason  # reuse the cancelled_reason field
    appt.cancelled_at = utc_now()
    _ = actor_user_id
    await session.flush()
    return appt


async def mark_appointment_rejected(
    session: AsyncSession,
    *,
    appointment_id: uuid.UUID,
    agency_id: uuid.UUID,
    payload: AppointmentRejectedRequest,
    actor_user_id: uuid.UUID | None = None,
) -> Appointment:
    """Mark an appointment as REJECTED (patient/guardian declined).

    Allowed only from `SCHEDULED`. After a visit has started the
    appointment is locked in — patient disagreement at that stage is
    surfaced via the signature / dispute flow, not REJECTED.
    """
    appt = await _get_appointment_or_404(
        session, appointment_id=appointment_id, agency_id=agency_id
    )

    if appt.status == AppointmentStatus.REJECTED:
        return appt  # idempotent

    if appt.status != AppointmentStatus.SCHEDULED:
        raise InvalidStateTransitionError(
            "Only SCHEDULED appointments can be marked REJECTED.",
            details={
                "current_status": appt.status.value,
                "requested_status": AppointmentStatus.REJECTED.value,
            },
        )

    appt.status = AppointmentStatus.REJECTED
    appt.cancelled_reason = payload.reason
    appt.cancelled_at = utc_now()
    _ = actor_user_id
    await session.flush()
    return appt


async def mark_appointment_billing_paid(
    session: AsyncSession,
    *,
    appointment_id: uuid.UUID,
    agency_id: uuid.UUID,
    by_user_id: uuid.UUID,
) -> Appointment:
    """Flip the denormalized billing toggle on the appointment row.

    The visit-side `billing_confirmed_at` is the staff's *caregiver*
    confirmation (timestamp of the clinical sign-off). This row is the
    *billing-payment* flag the agency admin / staff toggles after the
    payment is processed. Both can coexist: the FE renders one badge
    (`Paid` once `billing_status = 'paid'`).

    Idempotent — re-paying is a no-op (the original `billing_paid_at`
    is preserved for audit).
    """
    appt = await _get_appointment_or_404(
        session, appointment_id=appointment_id, agency_id=agency_id
    )

    if appt.billing_status == "paid":
        return appt  # idempotent

    appt.billing_status = "paid"
    appt.billing_paid_at = utc_now()
    appt.billing_paid_by_user_id = by_user_id
    await session.flush()
    return appt


async def sync_appointment_status_from_visit(
    session: AsyncSession,
    *,
    appointment_id: uuid.UUID,
    new_status: AppointmentStatus,
) -> None:
    """Mirror a Visit status change onto the parent Appointment row.

    Called from the visits service whenever a visit transitions. The
    mirror is only applied when the transition is legal in the
    appointment state machine; otherwise we silently keep the
    appointment row as-is (the visit row is the source of truth for
    in-flight activity).

    RLS: this function is invoked from the visits service which
    already opened a session under the caller's RLS context, so we
    don't re-assert agency_id here — RLS will simply no-op the update
    if the caller isn't permitted to see the row.
    """
    appt = await session.get(Appointment, appointment_id)
    if appt is None:
        return
    if appt.status == new_status:
        return
    if not _is_transition_allowed(appt.status, new_status):
        return  # appointment doesn't follow the same path; skip silently
    appt.status = new_status
    await session.flush()


async def assign_staff(
    session: AsyncSession,
    *,
    appointment_id: uuid.UUID,
    agency_id: uuid.UUID,
    staff_id: uuid.UUID,
) -> Appointment:
    """Assign (or re-assign) the staff member who will perform the visit.

    Allowed in pre-visit states only (SCHEDULED / READY). After the
    visit is in flight the staff is locked in.
    """
    appt = await _get_appointment_or_404(
        session, appointment_id=appointment_id, agency_id=agency_id
    )

    if appt.status in {
        AppointmentStatus.IN_PROGRESS,
        AppointmentStatus.AWAITING_SIGNATURE,
        AppointmentStatus.COMPLETED,
        AppointmentStatus.CANCELLED,
        AppointmentStatus.MISSED,
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
# Activities
# --------------------------------------------------------------------------
async def list_activities(
    session: AsyncSession,
    *,
    appointment_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> Sequence[AppointmentActivity]:
    """List activities for an appointment (oldest first)."""
    await _get_appointment_or_404(
        session, appointment_id=appointment_id, agency_id=agency_id
    )
    stmt = (
        select(AppointmentActivity)
        .where(
            AppointmentActivity.appointment_id == appointment_id,
            AppointmentActivity.agency_id == agency_id,
        )
        .order_by(AppointmentActivity.created_at.asc())
    )
    return (await session.execute(stmt)).scalars().all()


async def add_activity(
    session: AsyncSession,
    *,
    appointment_id: uuid.UUID,
    agency_id: uuid.UUID,
    payload: AppointmentActivityCreateRequest,
) -> AppointmentActivity:
    """Add a free-text activity to an existing appointment.

    Allowed only while the appointment is in a pre-visit state. After
    the visit is in progress, the caregiver updates each
    `VisitActivityDelivery` row instead — that becomes the source of
    truth for "what was actually delivered".
    """
    appt = await _get_appointment_or_404(
        session, appointment_id=appointment_id, agency_id=agency_id
    )
    if appt.status in {
        AppointmentStatus.IN_PROGRESS,
        AppointmentStatus.AWAITING_SIGNATURE,
        AppointmentStatus.COMPLETED,
        AppointmentStatus.CANCELLED,
        AppointmentStatus.MISSED,
        AppointmentStatus.REJECTED,
    }:
        raise InvalidStateTransitionError(
            "Cannot add activities to an in-flight or finalized appointment.",
            details={"current_status": appt.status.value},
        )

    activity = AppointmentActivity(
        appointment_id=appt.id,
        agency_id=agency_id,
        name=payload.name,
        planned_minutes=payload.planned_minutes,
        notes=payload.notes,
    )
    session.add(activity)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ValidationError(
            "Activity violates a check constraint.",
            details={"constraint": _extract_constraint(exc)},
        ) from exc
    return activity


async def update_activity(
    session: AsyncSession,
    *,
    activity_id: uuid.UUID,
    appointment_id: uuid.UUID,
    agency_id: uuid.UUID,
    payload: AppointmentActivityUpdateRequest,
) -> AppointmentActivity:
    """Patch an activity (name, planned_minutes, notes)."""
    activity = await _get_activity_or_404(
        session,
        activity_id=activity_id,
        appointment_id=appointment_id,
        agency_id=agency_id,
    )

    if payload.name is not None:
        activity.name = payload.name
    if payload.planned_minutes is not None:
        activity.planned_minutes = payload.planned_minutes
    if payload.notes is not None:
        activity.notes = payload.notes
    if payload.status is not None and payload.status != activity.status:
        if (
            activity.status == ServiceItemStatus.DONE
            and payload.status != ServiceItemStatus.DONE
        ):
            raise InvalidStateTransitionError(
                "Cannot move a DONE activity back to a non-final status.",
                details={"from": activity.status.value, "to": payload.status.value},
            )
        activity.status = payload.status

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ValidationError(
            "Activity update violates a check constraint.",
            details={"constraint": _extract_constraint(exc)},
        ) from exc
    return activity


async def delete_activity(
    session: AsyncSession,
    *,
    activity_id: uuid.UUID,
    appointment_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> None:
    """Remove an activity.

    Only allowed for PENDING activities — once an item has been
    delivered it's part of the clinical record.
    """
    activity = await _get_activity_or_404(
        session,
        activity_id=activity_id,
        appointment_id=appointment_id,
        agency_id=agency_id,
    )
    if activity.status != ServiceItemStatus.PENDING:
        raise InvalidStateTransitionError(
            "Cannot delete an activity that has been delivered.",
            details={"current_status": activity.status.value},
        )
    await session.delete(activity)
    await session.flush()


# --------------------------------------------------------------------------
# Summary serialization helper
# --------------------------------------------------------------------------
def _humanize_enum(value: object) -> str | None:
    """Render a `StrEnum` value as a human-readable label.

    Backwards-compat shim — prefer `humanize_enum` from
    `src.shared.utils.labels`.
    """
    from src.shared.utils.labels import humanize_enum

    return humanize_enum(value)


def _summarize_to_dict(appt: Appointment) -> dict:
    """Project a (eager-loaded) Appointment ORM row into the dict shape
    expected by `AppointmentSummaryResponse`.

    Safe to call even if `appt.staff` / `appt.patient` / `appt.activities`
    aren't eagerly loaded — all joined fields default to None.
    """
    from src.shared.utils.labels import (
        duration_label as _duration_label,
        patient_initials as _patient_initials,
    )

    # ----- Patient identity -----
    patient_name: str | None = None
    patient_initials: str | None = None
    patient_code: str | None = None
    patient = getattr(appt, "patient", None)
    if patient is not None:
        patient_code = getattr(patient, "patient_code", None)
        user = getattr(patient, "user", None)
        if user is not None:
            patient_name = getattr(user, "full_name", None)
            patient_initials = _patient_initials(patient_name)

    # ----- Caregiver -----
    staff_name: str | None = None
    staff_phone: str | None = None
    staff_code: str | None = None
    staff_credential: str | None = None
    staff = getattr(appt, "staff", None)
    if staff is not None:
        user = getattr(staff, "user", None)
        if user is not None:
            staff_name = getattr(user, "full_name", None)
            staff_phone = getattr(user, "phone", None)
        staff_code = getattr(staff, "staff_code", None)
        quals = getattr(staff, "qualifications", None) or []
        for q in quals:
            # First non-expired, non-rejected credential label.
            status = getattr(q, "status", None)
            label = getattr(q, "label", None) or getattr(q, "name", None)
            if label and (status is None or str(status).endswith("VERIFIED")):
                staff_credential = label
                break
        if staff_credential is None and quals:
            staff_credential = (
                getattr(quals[0], "label", None)
                or getattr(quals[0], "name", None)
            )

    # ----- Program + activity label -----
    program_name = _humanize_enum(getattr(appt, "program_type", None))
    activity_label: str | None = None
    activities = getattr(appt, "activities", None)
    if activities:
        first = activities[0]
        activity_label = getattr(first, "name", None)

    location_label = getattr(appt, "location", None)

    # ----- Duration (planned; actual EVV fallback done by the router) -----
    duration_str: str | None = None
    start = getattr(appt, "scheduled_start", None)
    end = getattr(appt, "scheduled_end", None)
    if start is not None and end is not None:
        delta = (end - start).total_seconds()
        duration_str = _duration_label(int(delta))

    return {
        "id": appt.id,
        "agency_id": appt.agency_id,
        "patient_id": appt.patient_id,
        "staff_id": appt.staff_id,
        "program_type": appt.program_type,
        "scheduled_start": appt.scheduled_start,
        "scheduled_end": appt.scheduled_end,
        "status": appt.status,
        "created_at": appt.created_at,
        "updated_at": appt.updated_at,
        "staff_name": staff_name,
        "staff_phone": staff_phone,
        "staff_code": staff_code,
        "staff_credential": staff_credential,
        "program_name": program_name,
        "service_type_label": activity_label,
        "location_label": location_label,
        "patient_name": patient_name,
        "patient_initials": patient_initials,
        "patient_code": patient_code,
        "duration_label": duration_str,
        "billing_status": getattr(appt, "billing_status", None) or "unpaid",
        "billing_paid_at": getattr(appt, "billing_paid_at", None),
        "claim_id": getattr(appt, "claim_id", None),
    }


__all__ = [
    "add_activity",
    "assign_staff",
    "cancel_appointment",
    "create_appointment",
    "delete_activity",
    "get_appointment",
    "list_activities",
    "list_appointments",
    "mark_appointment_billing_paid",
    "mark_appointment_missed",
    "mark_appointment_ready",
    "mark_appointment_rejected",
    "sync_appointment_status_from_visit",
    "transition_status",
    "update_activity",
    "update_appointment",
]
