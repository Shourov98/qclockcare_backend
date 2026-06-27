"""Visits service — business logic for visits, service items, notes,
verification, and issues.

Routes delegate here. This is the only place that composes ORM operations,
enforces business rules (state machine, NOT_DONE-reason validation,
verification-vs-dispute semantics, etc.), and raises the right domain
exceptions.

Visit lifecycle (see `VisitStatus`):

  CHECKED_IN → IN_PROGRESS → CHECKED_OUT → COMPLETED

Terminal: COMPLETED. Transitions back to CHECKED_IN are not allowed —
once checked out, the visit is in the post-service phase (verification
+ issues + billing pipeline).

RLS is the source of truth for tenant scoping; functions still take
`agency_id` for defence in depth.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.exceptions import (
    ConflictError,
    DuplicateResourceError,
    InvalidStateTransitionError,
    NotFoundError,
    ValidationError,
)
from src.modules.appointments.models import Appointment, AppointmentServiceItem
from src.modules.visits.models import (
    ServiceVerification,
    Visit,
    VisitIssue,
    VisitNote,
    VisitServiceItem,
)
from src.modules.visits.schemas import (
    ServiceVerificationCreateRequest,
    VisitCheckInRequest,
    VisitCheckOutRequest,
    VisitCreateRequest,
    VisitIssueCreateRequest,
    VisitIssueResolveRequest,
    VisitServiceItemCreateRequest,
    VisitServiceItemUpdateRequest,
    VisitStatusTransitionRequest,
)
from src.shared.domain.enums import (
    ServiceItemStatus,
    UserRole,
    VerificationStatus,
    VisitStatus,
)
from src.shared.utils.datetime_utils import utc_now

# --------------------------------------------------------------------------
# State machine (visit-level)
# --------------------------------------------------------------------------
_ALLOWED_TRANSITIONS: dict[VisitStatus, frozenset[VisitStatus]] = {
    VisitStatus.CHECKED_IN: frozenset({VisitStatus.IN_PROGRESS}),
    VisitStatus.IN_PROGRESS: frozenset({VisitStatus.CHECKED_OUT}),
    VisitStatus.CHECKED_OUT: frozenset({VisitStatus.COMPLETED}),
    VisitStatus.COMPLETED: frozenset(),  # terminal
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
) -> Visit:
    stmt = select(Visit).where(
        Visit.id == visit_id, Visit.agency_id == agency_id
    )
    if with_relations:
        stmt = stmt.options(
            selectinload(Visit.service_items),
            selectinload(Visit.notes),
            selectinload(Visit.verification),
            selectinload(Visit.issues),
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


async def _get_service_item_or_404(
    session: AsyncSession,
    *,
    item_id: uuid.UUID,
    visit_id: uuid.UUID,
) -> VisitServiceItem:
    stmt = select(VisitServiceItem).where(
        VisitServiceItem.id == item_id,
        VisitServiceItem.visit_id == visit_id,
    )
    i = (await session.execute(stmt)).scalar_one_or_none()
    if i is None:
        raise NotFoundError(
            details={"resource": "visit_service_item", "id": str(item_id)}
        )
    return i


async def _get_issue_or_404(
    session: AsyncSession,
    *,
    issue_id: uuid.UUID,
    visit_id: uuid.UUID,
) -> VisitIssue:
    stmt = select(VisitIssue).where(
        VisitIssue.id == issue_id, VisitIssue.visit_id == visit_id
    )
    i = (await session.execute(stmt)).scalar_one_or_none()
    if i is None:
        raise NotFoundError(details={"resource": "visit_issue", "id": str(issue_id)})
    return i


def _extract_constraint(exc: IntegrityError) -> str:
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    if diag is not None and getattr(diag, "constraint_name", None):
        return diag.constraint_name
    return "unknown"


# --------------------------------------------------------------------------
# Visits — CRUD
# --------------------------------------------------------------------------
async def create_visit(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    payload: VisitCreateRequest,
    created_by_user_id: uuid.UUID,
) -> Visit:
    """Create a new visit (typically on staff check-in).

    Verifies the appointment exists at the same agency and that the
    appointment already has a staff member assigned (we use that staff
    as the visit's `staff_id`). The visit row starts in CHECKED_IN
    with `check_in_time = now()`.
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

    # UNIQUE(appointment_id) constraint catches double-check-ins
    visit = Visit(
        appointment_id=appt.id,
        agency_id=agency_id,
        staff_id=appt.staff_id,
        status=VisitStatus.CHECKED_IN,
        check_in_time=utc_now(),
        check_in_lat=payload.check_in_lat,
        check_in_lng=payload.check_in_lng,
        check_in_accuracy_m=payload.check_in_accuracy_m,
        check_in_device_id=payload.check_in_device_id,
        check_in_address_match=payload.check_in_address_match,
        check_in_distance_from_location_m=payload.check_in_distance_from_location_m,
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

    # Optionally seed visit_service_items from the appointment's existing
    # service items (1:1 mapping). Callers can then mark each one DONE.
    for appt_item in appt.service_items:
        session.add(
            VisitServiceItem(
                visit_id=visit.id,
                appointment_service_item_id=appt_item.id,
            )
        )
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ValidationError(
            "Could not seed visit service items.",
            details={"constraint": _extract_constraint(exc)},
        ) from exc

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
    page: int = 1,
    page_size: int = 20,
) -> tuple[Sequence[Visit], int]:
    page = max(1, page)
    page_size = max(1, min(100, page_size))

    base = select(Visit).where(Visit.agency_id == agency_id)
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
        # Filter through the appointment join.
        from src.modules.appointments.models import Appointment

        base = base.join(
            Appointment, Appointment.id == Visit.appointment_id
        ).where(Appointment.patient_id == patient_id)
        count_base = count_base.join(
            Appointment, Appointment.id == Visit.appointment_id
        ).where(Appointment.patient_id == patient_id)
    if status_filter is not None:
        base = base.where(Visit.status == status_filter)
        count_base = count_base.where(Visit.status == status_filter)

    base = (
        base.order_by(Visit.check_in_time.desc(), Visit.id)
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    rows = (await session.execute(base)).scalars().all()
    total = (await session.execute(count_base)).scalar_one()
    return rows, int(total)


async def check_in_visit(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
    payload: VisitCheckInRequest,
) -> Visit:
    """Update the check-in fields on an existing visit (typically no-op
    if check-in was already recorded by `create_visit`).

    Only valid before check-out. After that, check-in info is locked.
    """
    visit = await _get_visit_or_404(
        session, visit_id=visit_id, agency_id=agency_id
    )
    if visit.status != VisitStatus.CHECKED_IN or visit.check_out_time is not None:
        raise InvalidStateTransitionError(
            "Cannot modify check-in after check-out.",
            details={"current_status": visit.status.value},
        )

    if payload.check_in_lat is not None:
        visit.check_in_lat = payload.check_in_lat
    if payload.check_in_lng is not None:
        visit.check_in_lng = payload.check_in_lng
    if payload.check_in_accuracy_m is not None:
        visit.check_in_accuracy_m = payload.check_in_accuracy_m
    if payload.check_in_device_id is not None:
        visit.check_in_device_id = payload.check_in_device_id
    if payload.check_in_address_match is not None:
        visit.check_in_address_match = payload.check_in_address_match
    if payload.check_in_distance_from_location_m is not None:
        visit.check_in_distance_from_location_m = (
            payload.check_in_distance_from_location_m
        )

    await session.flush()
    return visit


async def check_out_visit(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
    payload: VisitCheckOutRequest,
) -> Visit:
    """Record the actual check-out: stamps time, lat/lng, and duration."""
    visit = await _get_visit_or_404(
        session, visit_id=visit_id, agency_id=agency_id
    )
    if visit.check_in_time is None:
        raise ConflictError(
            "Cannot check out a visit that never checked in.",
            details={"visit_id": str(visit.id)},
        )
    if visit.check_out_time is not None:
        raise ConflictError(
            "Visit is already checked out.",
            details={"visit_id": str(visit.id), "check_out_time": visit.check_out_time.isoformat()},
        )
    if visit.status == VisitStatus.COMPLETED:
        raise InvalidStateTransitionError(
            "Cannot check out a visit that is already completed.",
            details={"current_status": visit.status.value},
        )

    now = utc_now()
    visit.check_out_time = now
    visit.check_out_lat = payload.check_out_lat
    visit.check_out_lng = payload.check_out_lng
    visit.check_out_accuracy_m = payload.check_out_accuracy_m
    visit.duration_seconds = int((now - visit.check_in_time).total_seconds())

    # Auto-add a note if one was provided alongside the check-out.
    if payload.note:
        # Caller's user id is not threaded through here; the router adds
        # the note as a separate request. This keeps the API simple.
        pass

    # Auto-progress to CHECKED_OUT (then COMPLETED on the next transition)
    visit.status = VisitStatus.CHECKED_OUT
    await session.flush()
    return visit


async def transition_visit_status(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
    payload: VisitStatusTransitionRequest,
) -> Visit:
    """Walk the visit lifecycle (CHECKED_IN → IN_PROGRESS → CHECKED_OUT → COMPLETED)."""
    visit = await _get_visit_or_404(
        session, visit_id=visit_id, agency_id=agency_id
    )

    if visit.status == payload.status:
        return visit

    if not _is_transition_allowed(visit.status, payload.status):
        raise InvalidStateTransitionError(
            f"Cannot transition visit from {visit.status.value} to {payload.status.value}.",
            details={"from": visit.status.value, "to": payload.status.value},
        )

    visit.status = payload.status
    await session.flush()
    return visit


# --------------------------------------------------------------------------
# Visit service items
# --------------------------------------------------------------------------
async def list_visit_service_items(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> Sequence[VisitServiceItem]:
    await _get_visit_or_404(session, visit_id=visit_id, agency_id=agency_id)
    stmt = (
        select(VisitServiceItem)
        .where(VisitServiceItem.visit_id == visit_id)
        .order_by(VisitServiceItem.created_at.asc())
    )
    return (await session.execute(stmt)).scalars().all()


async def add_visit_service_item(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
    payload: VisitServiceItemCreateRequest,
) -> VisitServiceItem:
    """Attach an additional appointment_service_item to a visit."""
    visit = await _get_visit_or_404(
        session, visit_id=visit_id, agency_id=agency_id
    )
    if visit.status == VisitStatus.COMPLETED:
        raise InvalidStateTransitionError(
            "Cannot add items to a completed visit.",
            details={"current_status": visit.status.value},
        )

    # The appointment_service_item must belong to this visit's appointment
    stmt = select(AppointmentServiceItem).where(
        AppointmentServiceItem.id == payload.appointment_service_item_id,
        AppointmentServiceItem.appointment_id == visit.appointment_id,
    )
    if (await session.execute(stmt)).scalar_one_or_none() is None:
        raise NotFoundError(
            details={
                "resource": "appointment_service_item",
                "id": str(payload.appointment_service_item_id),
            }
        )

    item = VisitServiceItem(
        visit_id=visit.id,
        appointment_service_item_id=payload.appointment_service_item_id,
        note=payload.note,
    )
    session.add(item)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise DuplicateResourceError(
            "This appointment_service_item is already attached to the visit.",
            details={"constraint": _extract_constraint(exc)},
        ) from exc
    return item


async def update_visit_service_item(
    session: AsyncSession,
    *,
    item_id: uuid.UUID,
    visit_id: uuid.UUID,
    payload: VisitServiceItemUpdateRequest,
    completed_by_user_id: uuid.UUID | None = None,
) -> VisitServiceItem:
    """Patch a visit service item (status / reason / note)."""
    item = await _get_service_item_or_404(
        session, item_id=item_id, visit_id=visit_id
    )

    if payload.status is not None and payload.status != item.status:
        # Don't allow rewinding a DONE item back to PENDING.
        if item.status == ServiceItemStatus.DONE and payload.status != ServiceItemStatus.DONE:
            raise InvalidStateTransitionError(
                "Cannot move a DONE service item back to a non-final status.",
                details={"from": item.status.value, "to": payload.status.value},
            )
        item.status = payload.status
        if payload.status == ServiceItemStatus.DONE:
            item.completed_at = utc_now()
            if completed_by_user_id is not None:
                item.completed_by = completed_by_user_id

    if payload.reason is not None:
        item.reason = payload.reason
    if payload.note is not None:
        item.note = payload.note

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ValidationError(
            "Visit service item update violates a check constraint.",
            details={"constraint": _extract_constraint(exc)},
        ) from exc
    return item


async def delete_visit_service_item(
    session: AsyncSession,
    *,
    item_id: uuid.UUID,
    visit_id: uuid.UUID,
) -> None:
    item = await _get_service_item_or_404(
        session, item_id=item_id, visit_id=visit_id
    )
    if item.status != ServiceItemStatus.PENDING:
        raise InvalidStateTransitionError(
            "Cannot delete a service item that has been delivered.",
            details={"current_status": item.status.value},
        )
    await session.delete(item)
    await session.flush()


# --------------------------------------------------------------------------
# Visit notes
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
        .where(VisitNote.visit_id == visit_id)
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
    note = VisitNote(
        visit_id=visit.id,
        author_user_id=author_user_id,
        body=body,
    )
    session.add(note)
    await session.flush()
    return note


# --------------------------------------------------------------------------
# Service verification
# --------------------------------------------------------------------------
async def get_or_create_verification(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
    verified_by: uuid.UUID,
    verifier_role: UserRole,
    payload: ServiceVerificationCreateRequest,
) -> ServiceVerification:
    """Create (or update if it already exists) the verification for a visit.

    The DB has UNIQUE(visit_id) on service_verifications, so there's
    exactly one row per visit. If one already exists, we update it —
    this supports the patient "I disputed, then changed my mind" flow.
    """
    if verifier_role not in {UserRole.PATIENT, UserRole.GUARDIAN}:
        raise ValidationError(
            "Only PATIENT or GUARDIAN may file a service verification.",
            details={"verifier_role": verifier_role.value},
        )

    visit = await _get_visit_or_404(
        session, visit_id=visit_id, agency_id=agency_id
    )

    existing = (
        await session.execute(
            select(ServiceVerification).where(
                ServiceVerification.visit_id == visit.id
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        # Update the existing verification
        if payload.status == VerificationStatus.DISPUTED and (
            payload.dispute_reason_code is None
            and existing.dispute_reason_code is None
        ):
            raise ValidationError(
                "dispute_reason_code is required when disputing.",
            )
        existing.status = payload.status
        existing.dispute_reason_code = payload.dispute_reason_code
        if payload.comment is not None:
            existing.comment = payload.comment
        await session.flush()
        return existing

    verification = ServiceVerification(
        visit_id=visit.id,
        agency_id=agency_id,
        verified_by=verified_by,
        verifier_role=verifier_role,
        status=payload.status,
        dispute_reason_code=payload.dispute_reason_code,
        comment=payload.comment,
    )
    session.add(verification)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ValidationError(
            "Verification violates a check constraint.",
            details={"constraint": _extract_constraint(exc)},
        ) from exc
    return verification


# --------------------------------------------------------------------------
# Visit issues
# --------------------------------------------------------------------------
async def list_visit_issues(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> Sequence[VisitIssue]:
    await _get_visit_or_404(session, visit_id=visit_id, agency_id=agency_id)
    stmt = (
        select(VisitIssue)
        .where(VisitIssue.visit_id == visit_id)
        .order_by(VisitIssue.created_at.asc())
    )
    return (await session.execute(stmt)).scalars().all()


async def add_visit_issue(
    session: AsyncSession,
    *,
    visit_id: uuid.UUID,
    agency_id: uuid.UUID,
    payload: VisitIssueCreateRequest,
    reported_by_user_id: uuid.UUID,
) -> VisitIssue:
    visit = await _get_visit_or_404(
        session, visit_id=visit_id, agency_id=agency_id
    )
    issue = VisitIssue(
        visit_id=visit.id,
        agency_id=agency_id,
        reported_by=reported_by_user_id,
        issue_type=payload.issue_type,
        comment=payload.comment,
    )
    session.add(issue)
    await session.flush()
    return issue


async def resolve_visit_issue(
    session: AsyncSession,
    *,
    issue_id: uuid.UUID,
    visit_id: uuid.UUID,
    payload: VisitIssueResolveRequest,
    resolved_by_user_id: uuid.UUID,
) -> VisitIssue:
    issue = await _get_issue_or_404(
        session, issue_id=issue_id, visit_id=visit_id
    )
    if issue.resolved_at is not None:
        # Idempotent — return as-is
        return issue
    issue.resolved_at = utc_now()
    issue.resolved_by = resolved_by_user_id
    issue.resolution_note = payload.resolution_note
    await session.flush()
    return issue


__all__ = [
    "add_visit_issue",
    "add_visit_note",
    "add_visit_service_item",
    "check_in_visit",
    "check_out_visit",
    "create_visit",
    "delete_visit_service_item",
    "get_or_create_verification",
    "get_visit",
    "list_visit_issues",
    "list_visit_notes",
    "list_visit_service_items",
    "list_visits",
    "resolve_visit_issue",
    "transition_visit_status",
    "update_visit_service_item",
]
