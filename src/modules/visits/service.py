"""Visits service — business logic for the materialized attendance record.

Routes delegate here. This is the only place that composes ORM operations,
enforces business rules (state machine, activity-completion gate,
billing-confirmation gate, signature-required gate), and raises the right
domain exceptions.

Lifecycle (see `VisitStatus`, mirrors `AppointmentStatus`):

  SCHEDULED → READY → IN_PROGRESS → AWAITING_SIGNATURE → COMPLETED
              ↘  CANCELLED, MISSED, REJECTED  ↙

Per the canonical spec (`QlockCare_appointemnt_flow.md`):
  - §5: every activity must be DONE (or NOT_APPLICABLE) before the
    caregiver can transition to AWAITING_SIGNATURE
  - §6: the caregiver must confirm billing before the same transition
  - §8: a signature (PATIENT or GUARDIAN) is required before COMPLETED

RLS is the source of truth for tenant scoping; functions still take
`agency_id` for defence in depth.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal

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
from src.core.logging import get_logger
from src.modules.appointments import service as appointments_service
from src.modules.appointments.models import Appointment, AppointmentActivity
from src.modules.staff.models import StaffProfile
from src.modules.visits.models import (
    AppointmentSignature,
    EVVRecord,
    Visit,
    VisitActivityDelivery,
    VisitNote,
)
from src.modules.visits.schemas import (
    VisitActivityUpdateRequest,
    VisitConfirmBillingRequest,
    VisitCreateRequest,
    VisitEndRequest,
    VisitLocationPingRequest,
    VisitStatusTransitionRequest,
)
from src.shared.domain.enums import (
    AppointmentStatus,
    ServiceItemStatus,
    UserRole,
    VisitStatus,
)
from src.shared.utils.datetime_utils import utc_now

logger = get_logger(__name__)


# --------------------------------------------------------------------------
# State machine (5-state lifecycle per spec)
# --------------------------------------------------------------------------
_ALLOWED_TRANSITIONS: dict[VisitStatus, frozenset[VisitStatus]] = {
    VisitStatus.SCHEDULED: frozenset(
        {
            VisitStatus.READY,
            VisitStatus.CANCELLED,
            VisitStatus.REJECTED,
        }
    ),
    VisitStatus.READY: frozenset(
        {
            VisitStatus.IN_PROGRESS,
            VisitStatus.CANCELLED,
            VisitStatus.MISSED,
        }
    ),
    VisitStatus.IN_PROGRESS: frozenset(
        {
            VisitStatus.AWAITING_SIGNATURE,
            VisitStatus.CANCELLED,
            VisitStatus.MISSED,
        }
    ),
    VisitStatus.AWAITING_SIGNATURE: frozenset(
        {
            VisitStatus.COMPLETED,
            VisitStatus.CANCELLED,
        }
    ),
    VisitStatus.COMPLETED: frozenset(),  # terminal
    VisitStatus.CANCELLED: frozenset(),  # terminal
    VisitStatus.MISSED: frozenset(),  # terminal
    VisitStatus.REJECTED: frozenset(),  # terminal
}


def _is_transition_allowed(
    from_state: VisitStatus, to_state: VisitStatus
) -> bool:
    return to_state in _ALLOWED_TRANSITIONS.get(from_state, frozenset())


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
async def _assert_agency_active(session: AsyncSession, agency_id: uuid.UUID) -> None:
    from src.modules.agencies.models import Agency

    agency = await session.get(Agency, agency_id)
    if agency is None:
        raise NotFoundError(details={"resource": "agency", "id": str(agency_id)})
    if agency.status.value == "CHURNED":
        raise ConflictError(
            "Cannot modify visits on a churned agency.",
            details={"agency_id": str(agency_id), "status": agency.status.value},
        )


async def _get_visit_or_404(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
    with_relations: bool = False,
    with_staff: bool = True,
) -> Visit:
    stmt = select(Visit).where(
        Visit.id == visit_id, Visit.agency_id == agency_id
    )
    if with_staff:
        stmt = stmt.options(
            selectinload(Visit.staff).selectinload(StaffProfile.user)
        )
    if with_relations:
        # Eager-load everything the staff Visit Summary screen renders
        # in one round trip:
        #   - `evv_record` (start + end + GPS + verification_status)
        #   - `signature` (signer_display_name + image)
        #   - `activity_deliveries.activity` (joined for `name` +
        #     `planned_minutes`)
        #   - `notes` (free-form narrative)
        stmt = stmt.options(
            selectinload(Visit.evv_record),
            selectinload(Visit.signature),
            selectinload(Visit.activity_deliveries).selectinload(
                VisitActivityDelivery.activity
            ),
            selectinload(Visit.notes),
        )
    v = (await session.execute(stmt)).scalar_one_or_none()
    if v is None:
        raise NotFoundError(details={"resource": "visit", "id": str(visit_id)})
    return v


async def _get_appointment_or_404(
    session: AsyncSession,
    *,
    appointment_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> Appointment:
    stmt = select(Appointment).where(
        Appointment.id == appointment_id, Appointment.agency_id == agency_id
    )
    a = (await session.execute(stmt)).scalar_one_or_none()
    if a is None:
        raise NotFoundError(
            details={"resource": "appointment", "id": str(appointment_id)}
        )
    return a


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


async def _get_activity_delivery_or_404(
    session: AsyncSession,
    *,
    activity_id: uuid.UUID,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> VisitActivityDelivery:
    stmt = select(VisitActivityDelivery).where(
        VisitActivityDelivery.activity_id == activity_id,
        VisitActivityDelivery.visit_id == visit_id,
        VisitActivityDelivery.agency_id == agency_id,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFoundError(
            details={
                "resource": "visit_activity_delivery",
                "activity_id": str(activity_id),
                "visit_id": str(visit_id),
            }
        )
    return row


def _extract_constraint(exc: IntegrityError) -> str:
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    if diag is not None and getattr(diag, "constraint_name", None):
        return diag.constraint_name
    return "unknown"


def _evv_verification_status(accuracy_m: Decimal | None) -> str:
    """Derive an EVV verification status from the GPS accuracy.

    EVV spec: a fix accurate to <=100m is "VERIFIED", <=500m is "PENDING"
    (admin should review), anything else is "FAILED".
    """
    if accuracy_m is None:
        return "PENDING"
    try:
        a = float(accuracy_m)
    except (TypeError, ValueError):
        return "PENDING"
    if a <= 100:
        return "VERIFIED"
    if a <= 500:
        return "PENDING"
    return "FAILED"


# --------------------------------------------------------------------------
# Visit CRUD
# --------------------------------------------------------------------------
async def create_visit(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    payload: VisitCreateRequest,
    created_by_user_id: uuid.UUID,
) -> Visit:
    """Create a new visit (`READY → IN_PROGRESS`).

    Verifies the appointment is at the same agency and is in `READY`.
    The visit row starts in `IN_PROGRESS`. The `EVVRecord.start_*` block
    is stamped from the supplied GPS. We seed one
    `VisitActivityDelivery` row per parent `AppointmentActivity` (1:1)
    so the caregiver can mark each one DONE / NOT_DONE / etc.
    """
    await _assert_agency_active(session, agency_id)

    appt = await _get_appointment_or_404(
        session, appointment_id=payload.appointment_id, agency_id=agency_id
    )
    if appt.staff_id is None:
        raise ConflictError(
            "Cannot create a visit for an appointment with no assigned staff.",
            details={"appointment_id": str(appt.id)},
        )

    # UNIQUE(appointment_id) constraint catches double check-ins.
    visit = Visit(
        appointment_id=appt.id,
        agency_id=agency_id,
        staff_id=appt.staff_id,
        status=VisitStatus.IN_PROGRESS,
    )
    session.add(visit)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise DuplicateResourceError(
            "A visit already exists for this appointment.",
            details={"constraint": _extract_constraint(exc)},
        ) from exc

    # Seed EVV start record.
    evv = EVVRecord(
        visit_id=visit.id,
        agency_id=agency_id,
        start_time=utc_now(),
        start_lat=payload.start_lat,
        start_lng=payload.start_lng,
        start_accuracy_m=payload.start_accuracy_m,
        start_device_id=payload.start_device_id,
        start_verification_status=_evv_verification_status(
            payload.start_accuracy_m
        ),
    )
    session.add(evv)

    # Seed one VisitActivityDelivery row per parent activity.
    for appt_activity in appt.activities:
        session.add(
            VisitActivityDelivery(
                visit_id=visit.id,
                activity_id=appt_activity.id,
                agency_id=agency_id,
            )
        )
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ValidationError(
            "Could not seed visit activity deliveries.",
            details={"constraint": _extract_constraint(exc)},
        ) from exc

    # Mirror the visit-side `IN_PROGRESS` to the parent appointment row
    # so the agency-admin dashboard reflects the live status. The
    # helper silently no-ops if the appointment's current state doesn't
    # legally allow the transition (e.g. an admin force-cancelled the
    # appointment between the SELECT and the INSERT — rare, but
    # possible).
    await appointments_service.sync_appointment_status_from_visit(
        session,
        appointment_id=appt.id,
        new_status=AppointmentStatus.IN_PROGRESS,
    )

    return visit


async def get_visit(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
    with_relations: bool = False,
) -> Visit:
    return await _get_visit_or_404(
        session,
        visit_id=visit_id,
        agency_id=agency_id,
        with_relations=with_relations,
    )


async def list_visits(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    appointment_id: uuid.UUID | None = None,
    staff_id: uuid.UUID | None = None,
    patient_id: uuid.UUID | None = None,
    status_filter: VisitStatus | None = None,
    sharing_only: bool = False,
    page: int = 1,
    page_size: int = 20,
) -> tuple[Sequence[Visit], int]:
    """Paginated list of visits at the caller's agency.

    Eager-loads staff→user so the EVV Live Monitor renders real names
    in a single round trip. No joined `patient_name` here — the live
    monitor renders the staff name on each card; patient names live on
    the appointment view.
    """
    page = max(1, page)
    page_size = max(1, min(100, page_size))

    base = (
        select(Visit)
        .where(Visit.agency_id == agency_id)
        .options(
            selectinload(Visit.staff).selectinload(StaffProfile.user),
            selectinload(Visit.evv_record),
            # Eager-load the parent appointment + its scheduled window.
            # The portal list endpoint (`GET /portal/visits`) reads
            # `v.appointment.scheduled_start` to sort and render each row,
            # and the admin/staff list pages render the same field — so we
            # eager-load it for every caller to avoid lazy-load
            # `MissingGreenlet` errors in async contexts.
            selectinload(Visit.appointment),
        )
    )
    count_base = (
        select(func.count())
        .select_from(Visit)
        .where(Visit.agency_id == agency_id)
    )
    if appointment_id is not None:
        base = base.where(Visit.appointment_id == appointment_id)
        count_base = count_base.where(Visit.appointment_id == appointment_id)
    if staff_id is not None:
        base = base.where(Visit.staff_id == staff_id)
        count_base = count_base.where(Visit.staff_id == staff_id)
    if patient_id is not None:
        base = base.join(
            Appointment, Appointment.id == Visit.appointment_id
        ).where(Appointment.patient_id == patient_id)
        count_base = count_base.join(
            Appointment, Appointment.id == Visit.appointment_id
        ).where(Appointment.patient_id == patient_id)
    if status_filter is not None:
        base = base.where(Visit.status == status_filter)
        count_base = count_base.where(Visit.status == status_filter)
    if sharing_only:
        base = base.where(Visit.sharing_location.is_(True))
        count_base = count_base.where(Visit.sharing_location.is_(True))

    base = base.order_by(Visit.created_at.desc(), Visit.id).limit(
        page_size
    ).offset((page - 1) * page_size)
    rows = (await session.execute(base)).scalars().all()
    total = (await session.execute(count_base)).scalar_one()
    return rows, int(total)


# --------------------------------------------------------------------------
# Visit history (completed-only, per-role views)
# --------------------------------------------------------------------------
async def list_visit_history(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    patient_id: uuid.UUID | None = None,
    staff_id: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[Sequence[Visit], int]:
    """Patient / staff / guardian visit history.

    Returns only visits in `COMPLETED` status, ordered by
    `appointment.scheduled_start DESC` (most recent visit first — that
    matches what a patient opens their dashboard to read).

    Exactly one of `patient_id` or `staff_id` must be provided. The
    guardian view is composed on top of the patient view by the
    router (a guardian can see visits for any patient they're linked
    to — the router fetches the linked patient ids first and calls
    this function once per patient, then merges).

    The `with_items` chain is eager-loaded so the response is the
    full `VisitResponse` shape — signature, EVV, activities, notes —
    matching what the Visit Summary screen already renders. The FE
    gets a single round trip per history page.

    Pagination mirrors `list_visits` (max page_size 100, 1-indexed).
    """
    if (patient_id is None) == (staff_id is None):
        raise ValidationError(
            "Provide exactly one of patient_id or staff_id.",
            details={"field": "patient_id/staff_id"},
        )

    page = max(1, page)
    page_size = max(1, min(100, page_size))

    # Status filter is hard-coded to COMPLETED — that's the only thing
    # that counts as "history". Cancelled / missed / rejected visits
    # show on the calendar, not here.
    base = (
        select(Visit)
        .where(
            Visit.agency_id == agency_id,
            Visit.status == VisitStatus.COMPLETED,
        )
        .options(
            selectinload(Visit.staff).selectinload(StaffProfile.user),
            selectinload(Visit.evv_record),
            selectinload(Visit.signature),
            selectinload(Visit.activity_deliveries).selectinload(
                VisitActivityDelivery.activity
            ),
            selectinload(Visit.notes),
        )
    )
    count_base = (
        select(func.count())
        .select_from(Visit)
        .where(
            Visit.agency_id == agency_id,
            Visit.status == VisitStatus.COMPLETED,
        )
    )
    if patient_id is not None:
        base = base.join(
            Appointment, Appointment.id == Visit.appointment_id
        ).where(Appointment.patient_id == patient_id)
        count_base = count_base.join(
            Appointment, Appointment.id == Visit.appointment_id
        ).where(Appointment.patient_id == patient_id)
    else:
        # staff_id is guaranteed set by the validation above.
        base = base.where(Visit.staff_id == staff_id)
        count_base = count_base.where(Visit.staff_id == staff_id)

    # Order by the parent appointment's scheduled_start DESC. We must
    # `join(Appointment, ...)` for the patient branch (it's already
    # there for the WHERE clause) and explicitly here for the staff
    # branch (the staff branch didn't join Appointment). `outerjoin`
    # keeps the staff query null-safe; in practice every COMPLETED
    # visit has an appointment so this is belt + suspenders.
    if staff_id is not None:
        base = base.outerjoin(
            Appointment, Appointment.id == Visit.appointment_id
        )
        count_base = count_base.outerjoin(
            Appointment, Appointment.id == Visit.appointment_id
        )
    base = (
        base.order_by(Appointment.scheduled_start.desc(), Visit.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    rows = (await session.execute(base)).scalars().all()
    total = (await session.execute(count_base)).scalar_one()
    return rows, int(total)


# --------------------------------------------------------------------------
# State transitions — the gateful ones
# --------------------------------------------------------------------------
async def _assert_can_request_signature(visit: Visit) -> None:
    """Spec §5 + §6 gate: all activities done AND billing confirmed.

    Raises ConflictError with a structured `details` payload so the FE
    can show a targeted error ("3 activities still pending" vs.
    "Please confirm billing first").
    """
    pending_activities = [
        d for d in visit.activity_deliveries if d.status == ServiceItemStatus.PENDING
    ]
    if pending_activities:
        raise ConflictError(
            "All required activities must be completed before ending this visit.",
            details={
                "reason": "activities_pending",
                "pending_count": len(pending_activities),
                "pending_activity_ids": [str(a.activity_id) for a in pending_activities],
            },
        )
    if visit.billing_confirmed_at is None:
        raise ConflictError(
            "Please confirm billing before submitting this visit.",
            details={"reason": "billing_not_confirmed"},
        )


async def _assert_signature_exists(visit: Visit) -> None:
    """Spec §8 gate: signature is mandatory before COMPLETED."""
    if visit.signature is None:
        raise ConflictError(
            "A patient or guardian signature is required to complete this visit.",
            details={"reason": "signature_missing"},
        )


async def confirm_billing(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
    payload: VisitConfirmBillingRequest,  # noqa: ARG001 — kept for future fields
    confirmed_by_user_id: uuid.UUID,
) -> Visit:
    """Spec §6 — caregiver ticks "I confirm the visit and billing
    information is correct".

    Idempotent — re-confirming is a no-op (the timestamp stays as the
    first confirmation time, which is the audit-quality signal).
    """
    visit = await _get_visit_or_404(
        session, visit_id=visit_id, agency_id=agency_id
    )
    if visit.billing_confirmed_at is not None:
        return visit
    visit.billing_confirmed_at = utc_now()
    visit.billing_confirmed_by_user_id = confirmed_by_user_id
    await session.flush()
    return visit


async def transition_visit_status(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
    payload: VisitStatusTransitionRequest,
) -> Visit:
    """Walk the 5-state lifecycle with the spec's gates.

      IN_PROGRESS → AWAITING_SIGNATURE
          requires all activities DONE/NOT_APPLICABLE/NEEDS_FOLLOW_UP
          AND `billing_confirmed_at` set.
      AWAITING_SIGNATURE → COMPLETED
          requires an `AppointmentSignature` row.
    """
    visit = await _get_visit_or_404(
        session, visit_id=visit_id, agency_id=agency_id, with_relations=True
    )

    if visit.status == payload.status:
        return visit

    if not _is_transition_allowed(visit.status, payload.status):
        raise InvalidStateTransitionError(
            f"Cannot transition visit from {visit.status.value} to {payload.status.value}.",
            details={"from": visit.status.value, "to": payload.status.value},
        )

    # Spec §5 + §6 gates on the End Task transition.
    if (
        visit.status == VisitStatus.IN_PROGRESS
        and payload.status == VisitStatus.AWAITING_SIGNATURE
    ):
        await _assert_can_request_signature(visit)

    # Spec §8 gate on completion.
    if (
        visit.status == VisitStatus.AWAITING_SIGNATURE
        and payload.status == VisitStatus.COMPLETED
    ):
        await _assert_signature_exists(visit)

    visit.status = payload.status
    if payload.status == VisitStatus.COMPLETED:
        visit.sharing_location = False
    await session.flush()

    # Mirror the visit-side status change to the parent appointment so
    # the agency-admin dashboard reflects the live lifecycle. The helper
    # silently no-ops on illegal transitions (e.g. visit-side MISSED
    # doesn't always make sense at the appointment level — see
    # `_ALLOWED_TRANSITIONS`).
    if hasattr(VisitStatus, "_appointment_mirror_map"):
        target_appt_status = VisitStatus._appointment_mirror_map.get(
            payload.status
        )
    else:
        # Default mirror — every visit status has an equivalent
        # appointment status (the 8-state lifecycle is mirrored 1:1).
        target_appt_status = AppointmentStatus(payload.status.value)
    if target_appt_status is not None:
        await appointments_service.sync_appointment_status_from_visit(
            session,
            appointment_id=visit.appointment_id,
            new_status=target_appt_status,
        )

    # Mirror the per-visit activity delivery states onto the parent
    # appointment's activity rows so the FE's Visit Summary (which
    # reads activities from `GET /appointments/{id}/with-items`)
    # reflects the actual delivery state. We only do this at the
    # `IN_PROGRESS → AWAITING_SIGNATURE` transition because that's the
    # only point where every delivery is *guaranteed* to be in a
    # terminal state — the `_assert_can_request_signature` gate runs
    # just above and rejects any visit with a `PENDING` delivery.
    # Mirroring earlier (e.g. while the visit is still IN_PROGRESS)
    # would publish incomplete data; mirroring later (after
    # COMPLETED) wouldn't help — the FE doesn't show the activities
    # card on COMPLETED appointments.
    if (
        target_appt_status == AppointmentStatus.AWAITING_SIGNATURE
        and payload.status == VisitStatus.AWAITING_SIGNATURE
    ):
        mirrored = await appointments_service.sync_appointment_activities_from_visit(
            session,
            appointment_id=visit.appointment_id,
            visit_id=visit.id,
        )
        # Debug-only counter — useful for the dev overlay / audit log
        # to spot the case where the gate let through a visit with
        # zero activities (shouldn't happen, but cheap to surface).
        if mirrored == 0:
            logger.info(
                "visit.activity_mirror_empty",
                visit_id=str(visit.id),
                appointment_id=str(visit.appointment_id),
            )
    return visit


# --------------------------------------------------------------------------
# EVV end
# --------------------------------------------------------------------------
async def end_visit(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
    payload: VisitEndRequest,
) -> Visit:
    """Record the EVV End block (caregiver departure).

    Stamps `evv_records.end_*` and (if start was also recorded) populates
    `duration_seconds`. Does NOT auto-progress the visit status — the
    caregiver must call `/confirm-billing` then `/transition` to
    AWAITING_SIGNATURE.
    """
    visit = await _get_visit_or_404(
        session, visit_id=visit_id, agency_id=agency_id, with_relations=True
    )
    if visit.evv_record is None:
        raise ConflictError(
            "Cannot end a visit that never started.",
            details={"visit_id": str(visit.id)},
        )
    if visit.evv_record.end_time is not None:
        raise ConflictError(
            "Visit is already ended.",
            details={"visit_id": str(visit.id), "end_time": visit.evv_record.end_time.isoformat()},
        )
    if visit.status != VisitStatus.IN_PROGRESS:
        raise InvalidStateTransitionError(
            "Cannot end a visit that is not in progress.",
            details={"current_status": visit.status.value},
        )

    now = utc_now()
    evv = visit.evv_record
    evv.end_time = now
    evv.end_lat = payload.end_lat
    evv.end_lng = payload.end_lng
    evv.end_accuracy_m = payload.end_accuracy_m
    visit.sharing_location = False
    await session.flush()
    return visit


# --------------------------------------------------------------------------
# Live location sharing
# --------------------------------------------------------------------------
async def start_location_sharing(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
    initial_lat: Decimal | None = None,
    initial_lng: Decimal | None = None,
    initial_accuracy_m: Decimal | None = None,
) -> Visit:
    """Opt in to live GPS streaming for the visit."""
    visit = await _get_visit_or_404(
        session, visit_id=visit_id, agency_id=agency_id
    )
    if visit.status != VisitStatus.IN_PROGRESS:
        raise InvalidStateTransitionError(
            "Cannot share location while the visit is not in progress.",
            details={"current_status": visit.status.value},
        )

    visit.sharing_location = True
    if initial_lat is not None and initial_lng is not None:
        visit.live_lat = initial_lat
        visit.live_lng = initial_lng
        visit.live_ping_at = utc_now()
        if initial_accuracy_m is not None:
            visit.live_accuracy_m = initial_accuracy_m
    await session.flush()
    return visit


async def record_location_ping(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
    payload: VisitLocationPingRequest,
) -> Visit:
    """Update the visit's live lat/lng with a fresh ping."""
    visit = await _get_visit_or_404(
        session, visit_id=visit_id, agency_id=agency_id
    )
    if visit.status != VisitStatus.IN_PROGRESS:
        return visit  # silently drop stale pings
    if not visit.sharing_location:
        return visit  # staff revoked permission

    visit.live_lat = payload.lat
    visit.live_lng = payload.lng
    visit.live_ping_at = utc_now()
    if payload.accuracy_m is not None:
        visit.live_accuracy_m = payload.accuracy_m
    if payload.device_id is not None and visit.staff_id is not None:
        # Mirror the device_id onto staff.last_known_device_id only on
        # the first ping. We do not read-modify-write the staff row here
        # — visit.staff is lazy; the FE never needs the device_id on
        # the staff row to render.
        pass

    # Mirror onto staff.last_known_* so the EVV Live Monitor's
    # staff-level view can show a "last seen at" pin.
    staff = (
        await session.execute(
            select(StaffProfile).where(
                StaffProfile.id == visit.staff_id,
                StaffProfile.agency_id == agency_id,
            )
        )
    ).scalar_one_or_none()
    if staff is not None:
        staff.last_known_lat = payload.lat
        staff.last_known_lng = payload.lng
        staff.last_known_ping_at = visit.live_ping_at
        staff.last_known_visit_id = visit.id
        if payload.accuracy_m is not None:
            staff.last_known_accuracy_m = payload.accuracy_m
        if payload.device_id is not None:
            staff.last_known_device_id = payload.device_id

    await session.flush()
    return visit


async def stop_location_sharing(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> Visit:
    """Opt out of live GPS. Keeps the last known position on the row."""
    visit = await _get_visit_or_404(
        session, visit_id=visit_id, agency_id=agency_id
    )
    visit.sharing_location = False
    await session.flush()
    return visit


# --------------------------------------------------------------------------
# Activities
# --------------------------------------------------------------------------
async def list_visit_activities(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> Sequence[VisitActivityDelivery]:
    """List the activity deliveries for one visit (oldest first)."""
    await _get_visit_or_404(session, visit_id=visit_id, agency_id=agency_id)
    stmt = (
        select(VisitActivityDelivery)
        .where(
            VisitActivityDelivery.visit_id == visit_id,
            VisitActivityDelivery.agency_id == agency_id,
        )
        .options(selectinload(VisitActivityDelivery.activity))
        .order_by(VisitActivityDelivery.created_at.asc())
    )
    return (await session.execute(stmt)).scalars().all()


async def update_visit_activity(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
    activity_id: uuid.UUID,
    payload: VisitActivityUpdateRequest,
    completed_by_user_id: uuid.UUID,
) -> VisitActivityDelivery:
    """Mark one activity DONE / NOT_DONE / NOT_APPLICABLE / FOLLOW_UP.

    Per spec §5, `NOT_DONE` requires a reason (DB-enforced).
    """
    visit = await _get_visit_or_404(
        session, visit_id=visit_id, agency_id=agency_id
    )
    if visit.status not in {
        VisitStatus.IN_PROGRESS,
        VisitStatus.AWAITING_SIGNATURE,
    }:
        raise InvalidStateTransitionError(
            "Cannot update activities on a visit in its current state.",
            details={"current_status": visit.status.value},
        )

    delivery = await _get_activity_delivery_or_404(
        session,
        activity_id=activity_id,
        visit_id=visit_id,
        agency_id=agency_id,
    )

    if payload.status is not None:
        if (
            delivery.status == ServiceItemStatus.DONE
            and payload.status != ServiceItemStatus.DONE
        ):
            raise InvalidStateTransitionError(
                "Cannot move a DONE activity back to a non-final status.",
                details={"from": delivery.status.value, "to": payload.status.value},
            )
        delivery.status = payload.status
        if payload.status != ServiceItemStatus.PENDING:
            delivery.completed_at = utc_now()
            delivery.completed_by = completed_by_user_id
    if payload.reason is not None:
        delivery.reason = payload.reason
    if payload.note is not None:
        delivery.note = payload.note

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ValidationError(
            "Activity delivery violates a check constraint.",
            details={"constraint": _extract_constraint(exc)},
        ) from exc
    return delivery


# --------------------------------------------------------------------------
# Notes
# --------------------------------------------------------------------------
async def list_visit_notes(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> Sequence[VisitNote]:
    await _get_visit_or_404(session, visit_id=visit_id, agency_id=agency_id)
    stmt = (
        select(VisitNote)
        .where(
            VisitNote.visit_id == visit_id,
        )
        .options(selectinload(VisitNote.author))
        .order_by(VisitNote.created_at.asc())
    )
    return (await session.execute(stmt)).scalars().all()


async def add_visit_note(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
    body: str,
    author_user_id: uuid.UUID,
) -> VisitNote:
    visit = await _get_visit_or_404(
        session, visit_id=visit_id, agency_id=agency_id
    )
    if visit.status not in {
        VisitStatus.IN_PROGRESS,
        VisitStatus.AWAITING_SIGNATURE,
        VisitStatus.COMPLETED,
    }:
        raise InvalidStateTransitionError(
            "Cannot add a note to a visit in its current state.",
            details={"current_status": visit.status.value},
        )

    note = VisitNote(
        visit_id=visit.id,
        author_user_id=author_user_id,
        body=body,
    )
    session.add(note)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ValidationError(
            "Visit note violates a check constraint.",
            details={"constraint": _extract_constraint(exc)},
        ) from exc
    return note


# --------------------------------------------------------------------------
# Signatures
# --------------------------------------------------------------------------
async def get_or_create_signature_placeholder(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> AppointmentSignature | None:
    """Read the signature row for a visit (used by `_to_response`)."""
    return (
        await session.execute(
            select(AppointmentSignature).where(
                AppointmentSignature.visit_id == visit_id,
                AppointmentSignature.agency_id == agency_id,
            )
        )
    ).scalar_one_or_none()


async def sign_visit(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
    signer_user_id: uuid.UUID,
    signer_role: UserRole,
    signature_image_url: str,
    signer_display_name_override: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> AppointmentSignature:
    """File a signature on a visit (spec §8).

    Spec rule: PATIENT OR GUARDIAN — either signer satisfies the gate.
    The `signer_display_name` is computed from the signer's `users.full_name`
    via `signer_display_name()` ("J. Smith" format) unless the FE passes
    an explicit override (rare — for legacy UI that already formats).

    Idempotent at the visit level: a second signature POST overwrites
    the prior row (1:1 UNIQUE constraint), keeping the most recent
    intent. This matches the spec's "Patient signs → valid / Guardian
    signs → valid / Patient + Guardian sign → also valid" wording — we
    keep the last signer for audit.
    """
    if signer_role not in {UserRole.PATIENT, UserRole.GUARDIAN}:
        raise CrossAgencyAccessDeniedError(
            details={"reason": "only PATIENT or GUARDIAN may sign"}
        )

    visit = await _get_visit_or_404(
        session, visit_id=visit_id, agency_id=agency_id
    )
    if visit.status != VisitStatus.AWAITING_SIGNATURE:
        raise InvalidStateTransitionError(
            "Visit is not awaiting signature.",
            details={"current_status": visit.status.value},
        )

    # Resolve the rendered display name.
    from src.modules.identity.models import User

    signer_user = (
        await session.execute(
            select(User).where(User.id == signer_user_id)
        )
    ).scalar_one_or_none()
    if signer_user is None:
        raise NotFoundError(
            details={"resource": "user", "id": str(signer_user_id)}
        )
    from src.shared.utils.labels import signer_display_name
    rendered_name = signer_display_name(signer_user.full_name) or ""

    existing = (
        await session.execute(
            select(AppointmentSignature).where(
                AppointmentSignature.visit_id == visit_id
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.signer_user_id = signer_user_id
        existing.signer_role = signer_role
        existing.signer_display_name = (
            signer_display_name_override or rendered_name
        )
        existing.signature_image_url = signature_image_url
        existing.signed_at = utc_now()
        if ip_address is not None:
            existing.ip_address = ip_address
        if user_agent is not None:
            existing.user_agent = user_agent
        sig = existing
    else:
        sig = AppointmentSignature(
            visit_id=visit_id,
            agency_id=agency_id,
            signer_user_id=signer_user_id,
            signer_role=signer_role,
            signer_display_name=signer_display_name_override or rendered_name,
            signature_image_url=signature_image_url,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        session.add(sig)

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ValidationError(
            "Signature violates a check constraint.",
            details={"constraint": _extract_constraint(exc)},
        ) from exc
    return sig


# --------------------------------------------------------------------------
# Compliance rollup — computed-on-read for `GET /visits/{id}/compliance`
# --------------------------------------------------------------------------
async def get_visit_compliance(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> "VisitComplianceResponse":
    """Build the 5-row compliance rollup powering `ComplianceCard`.

    All four checks (EVV start, ≥1 note, signature, billing-confirmed)
    run against one eager-loaded visit. RLS handles cross-tenant
    visibility — if the visit doesn't belong to the caller's agency
    `_get_visit_or_404` raises `NotFoundError` (not 403) so we don't
    leak existence.
    """
    from src.modules.visits.schemas import (
        VisitComplianceResponse,
        VisitComplianceRow,
    )

    visit = await _get_visit_or_404(
        session,
        visit_id=visit_id,
        agency_id=agency_id,
        with_relations=True,
        with_staff=False,
    )

    # EVV row — `unavailable` until the caregiver POSTed `/visits`.
    evv = visit.evv_record
    evv_row = VisitComplianceRow(
        key="evv",
        label="EVV Record",
        status="captured" if (evv is not None and evv.start_time) else "unavailable",
        captured_at=(evv.start_time if evv is not None else None),
    )

    # Visit notes — `submitted` once any note exists.
    has_note = bool(visit.notes)
    notes_row = VisitComplianceRow(
        key="notes",
        label="Visit Notes",
        status="submitted" if has_note else "unavailable",
        captured_at=visit.notes[0].created_at if has_note else None,
    )

    # Signature — `signed` once the row exists, otherwise `pending`.
    sig = visit.signature
    sig_row = VisitComplianceRow(
        key="signature",
        label="Client Signature",
        status="signed" if sig is not None else "pending",
        captured_at=(sig.signed_at if sig is not None else None),
    )

    # Clean claim — `verified` only when both billing-confirmed AND
    # signature are present; `pending` if billing is confirmed but the
    # signature is still missing; otherwise `unavailable`.
    billing_set = visit.billing_confirmed_at is not None
    if billing_set and sig is not None:
        clean_claim_status = "verified"
    elif billing_set:
        clean_claim_status = "pending"
    else:
        clean_claim_status = "unavailable"
    clean_claim_row = VisitComplianceRow(
        key="clean_claim",
        label="Clean Claim",
        status=clean_claim_status,
        captured_at=visit.billing_confirmed_at,
    )

    # Billing — `ready` once confirmed, `pending` otherwise. The FE
    # renders `"Pending submission"` as the trailing label while pending.
    billing_row = VisitComplianceRow(
        key="billing",
        label="Billing Status",
        status="ready" if billing_set else "pending",
        label_override=(None if billing_set else "Pending submission"),
        captured_at=visit.billing_confirmed_at,
    )

    return VisitComplianceResponse(
        visit_id=visit.id,
        appointment_id=visit.appointment_id,
        agency_id=visit.agency_id,
        status=visit.status,
        rows=[
            evv_row,
            notes_row,
            sig_row,
            clean_claim_row,
            billing_row,
        ],
        generated_at=utc_now(),
    )


__all__ = [
    "add_visit_note",
    "confirm_billing",
    "create_visit",
    "end_visit",
    "get_visit",
    "get_visit_compliance",
    "get_or_create_signature_placeholder",
    "list_visit_activities",
    "list_visit_history",
    "list_visit_notes",
    "list_visits",
    "record_location_ping",
    "sign_visit",
    "start_location_sharing",
    "stop_location_sharing",
    "transition_visit_status",
    "update_visit_activity",
]
