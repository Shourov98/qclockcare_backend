"""Appointments router — `/appointments` and `/appointments/{id}/...`.

All routes require authentication. State-mutating routes (create, update,
cancel, transition, ready, missed, rejected, billing, activity writes)
require AGENCY_ADMIN (with `STAFF` permitted on the billing toggle).
Read routes are open to AGENCY_ADMIN, STAFF, the patient themselves,
and authorised guardians (RLS narrows the rows).

Endpoints:
  POST   /appointments                                — schedule visit
  GET    /appointments                                — list (paginated, filterable)
  GET    /appointments/{id}                           — fetch (summary)
  GET    /appointments/{id}/with-items                — fetch + nested activities
  PATCH  /appointments/{id}                           — patch window / staff / notes
  POST   /appointments/{id}/cancel                    — cancel (pre-visit only)
  POST   /appointments/{id}/transition                — status transition (state machine)
  POST   /appointments/{id}/ready                     — admin marks READY (SCHEDULED→READY)
  POST   /appointments/{id}/missed                    — mark MISSED (SCHEDULED/READY)
  POST   /appointments/{id}/rejected                  — mark REJECTED (SCHEDULED)
  POST   /appointments/{id}/assign                    — assign staff
  POST   /appointments/{id}/billing/paid              — flip billing toggle to paid

  GET    /appointments/{id}/activities
  POST   /appointments/{id}/activities
  PATCH  /appointments/{id}/activities/{activity_id}
  DELETE /appointments/{id}/activities/{activity_id}
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import (
    CrossAgencyAccessDeniedError,
    ForbiddenError,
    ValidationError,
)
from src.core.logging import get_logger
from src.modules.appointments import service as appointments_service
from src.modules.appointments.models import Appointment
from src.modules.appointments.schemas import (
    AppointmentActivityCreateRequest,
    AppointmentActivityResponse,
    AppointmentActivityUpdateRequest,
    AppointmentCancelRequest,
    AppointmentCreateRequest,
    AppointmentMarkBillingPaidRequest,
    AppointmentMissedRequest,
    AppointmentReadyRequest,  # noqa: F401 — re-exported below
    AppointmentRejectedRequest,
    AppointmentResponse,
    AppointmentStatusTransitionRequest,
    AppointmentSummaryResponse,
    AppointmentUpdateRequest,
    CalendarAppointmentsResponse,
)
from src.modules.audit_logs import service as audit_logs_service
from src.modules.identity.dependencies import (
    CurrentAuth,
    get_session_with_auth,
    require_role,
)
from src.shared.domain.enums import AppointmentStatus, AuditAction, UserRole
from src.shared.schemas.docs import standard_responses
from src.shared.schemas.pagination import build_offset_response

logger = get_logger(__name__)

router = APIRouter(prefix="/appointments", tags=["appointments"])


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _require_agency(ctx: CurrentAuth) -> uuid.UUID:
    """All appointment routes need an agency context. SUPER_ADMIN rejected."""
    if ctx.role == UserRole.SUPER_ADMIN:
        raise ForbiddenError(
            "Use the platform admin console for cross-agency appointment operations."
        )
    if ctx.agency_id is None:
        raise ForbiddenError("Caller has no agency context.")
    return ctx.agency_id


def _ensure_can_view(ctx: CurrentAuth, patient_user_id: uuid.UUID) -> None:
    """Visibility rules:
    - AGENCY_ADMIN: always (within their agency)
    - STAFF: always (within their agency — they may need to see the calendar)
    - PATIENT: only their own appointments
    - GUARDIAN: only for patients they're linked to (service-layer RLS enforces;
      here we just block at the role level — the DB RLS will reject if not linked)
    """
    if ctx.role in {UserRole.AGENCY_ADMIN, UserRole.STAFF}:
        return
    if ctx.role == UserRole.PATIENT:
        if ctx.user_id != patient_user_id:
            raise CrossAgencyAccessDeniedError()
        return
    if ctx.role == UserRole.GUARDIAN:
        # RLS will block guardian rows they don't have a link for; allow
        # the request through and let the policy do the filtering.
        return
    raise CrossAgencyAccessDeniedError()


def _to_response(
    appt: object,
    *,
    with_items: bool = False,
) -> AppointmentResponse:
    """Build an AppointmentResponse without triggering lazy loads.

    `Appointment.activities` is a lazy-loaded relationship. We only
    include nested items when explicitly requested AND the collection has
    been eager-loaded by the service.

    `Appointment.location_rel` is `lazy="raise"` so we always have to
    project it explicitly via `getattr(..., None)` (the service-layer
    eagers, when used, leave the attr as `None` on rows without a
    location row).
    """
    location_rel = getattr(appt, "location_rel", None)
    latitude = (
        float(location_rel.latitude)
        if location_rel is not None and location_rel.latitude is not None
        else None
    )
    longitude = (
        float(location_rel.longitude)
        if location_rel is not None and location_rel.longitude is not None
        else None
    )
    location_name = (
        getattr(location_rel, "label", None) if location_rel is not None else None
    )
    line1 = (
        getattr(location_rel, "address_line1", None)
        if location_rel is not None
        else None
    )
    city = (
        getattr(location_rel, "city", None) if location_rel is not None else None
    )
    state = (
        getattr(location_rel, "state", None) if location_rel is not None else None
    )
    parts = [p for p in (line1, city, state) if p]
    location_address = ", ".join(parts) if parts else None

    data: dict = {
        "id": appt.id,
        "agency_id": appt.agency_id,
        "patient_id": appt.patient_id,
        "staff_id": appt.staff_id,
        "program_type": appt.program_type,
        "scheduled_start": appt.scheduled_start,
        "scheduled_end": appt.scheduled_end,
        "status": appt.status,
        "location": appt.location,
        "notes": appt.notes,
        "cancelled_reason": appt.cancelled_reason,
        "cancelled_at": appt.cancelled_at,
        "created_at": appt.created_at,
        "updated_at": appt.updated_at,
        "location_id": getattr(appt, "location_id", None),
        "latitude": latitude,
        "longitude": longitude,
        "location_name": location_name,
        "location_address": location_address,
    }
    if with_items:
        try:
            data["activities"] = list(appt.activities)
        except Exception:
            data["activities"] = None
        # Signature lives on `appointment.visit.signature` — only
        # available when the visit exists AND was signed. We guard
        # each attribute separately because (a) the visit may exist
        # but have no signature yet (in-flight), and (b) the
        # relationship is lazy unless explicitly eager-loaded — and
        # `get_appointment_with_items_endpoint` does load it. For
        # other callers of `_to_response(..., with_items=True)` the
        # visit object won't have `signature` populated and we'd hit
        # `None` here.
        try:
            data["signature"] = (
                appt.visit.signature if appt.visit is not None else None
            )
        except Exception:
            data["signature"] = None
    else:
        data["activities"] = None
        data["signature"] = None
    return AppointmentResponse.model_validate(data)


# --------------------------------------------------------------------------
# Appointment CRUD
# --------------------------------------------------------------------------
@router.post(
    "",
    response_model=AppointmentResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def create_appointment_endpoint(
    payload: AppointmentCreateRequest,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> AppointmentResponse:
    """Schedule a new appointment at the caller's agency.

    New rows start in `SCHEDULED`. The admin marks them `READY` via
    `POST /appointments/{id}/ready` once the caregiver is notified.
    """
    agency_id = _require_agency(ctx)
    appt = await appointments_service.create_appointment(
        session,
        agency_id=agency_id,
        payload=payload,
        scheduled_by_user_id=ctx.user_id,
    )
    await session.commit()
    await session.refresh(appt)
    # Best-effort audit log.
    try:
        ip, ua = audit_logs_service.request_ip_ua(request)
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.APPOINTMENT_CREATED,
            entity_type="APPOINTMENT",
            entity_id=appt.id,
            new_data={
                "patient_id": str(appt.patient_id),
                "staff_id": str(appt.staff_id) if appt.staff_id else None,
                "program_type": appt.program_type.value
                if hasattr(appt.program_type, "value")
                else str(appt.program_type),
                "scheduled_start": appt.scheduled_start.isoformat()
                if appt.scheduled_start
                else None,
                "scheduled_end": appt.scheduled_end.isoformat()
                if appt.scheduled_end
                else None,
            },
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    return _to_response(appt, with_items=True)


@router.get(
    "",
    response_model=dict,
)
async def list_appointments_endpoint(
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    patient_id: uuid.UUID | None = Query(default=None),
    staff_id: uuid.UUID | None = Query(default=None),
    status_filter: AppointmentStatus | None = Query(default=None, alias="status"),
    date_from: date | None = Query(
        default=None,
        description="Calendar date (YYYY-MM-DD) — only appointments at or after this date are returned.",
    ),
    date_to: date | None = Query(
        default=None,
        description="Calendar date (YYYY-MM-DD, inclusive) — only appointments on or before this date are returned.",
    ),
    page: int = Query(default=1, ge=1, le=10000),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    """Paginated list of appointments at the caller's agency.

    Optional filters:
      - patient_id (PATIENT role forces this to their own profile id)
      - staff_id   (STAFF role: useful for "my day" / "my week" views)
      - status     — one of the 8 lifecycle values
      - date_from / date_to — calendar-date filters on `scheduled_start`,
                              both inclusive (so `date_from == date_to`
                              means "appointments on that day")

    RLS automatically restricts PATIENT/GUARDIAN rows to their own
    appointments; here we additionally force PATIENT to filter by
    themselves so the SQL is stable.
    """
    agency_id = _require_agency(ctx)

    # Force patient scope if the caller is a PATIENT
    if ctx.role == UserRole.PATIENT:
        # Look up the patient_profile.id for the current user
        from sqlalchemy import select

        from src.modules.patients.models import PatientProfile

        stmt = select(PatientProfile.id).where(
            PatientProfile.user_id == ctx.user_id,
            PatientProfile.agency_id == agency_id,
        )
        own_patient_id = (await session.execute(stmt)).scalar_one_or_none()
        if own_patient_id is None:
            return build_offset_response([], total=0, page=page, page_size=page_size)
        patient_id = own_patient_id

    rows, total = await appointments_service.list_appointments(
        session,
        agency_id=agency_id,
        patient_id=patient_id,
        staff_id=staff_id,
        status_filter=status_filter,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size,
    )
    # `list_appointments` eager-loads `staff.user`, `staff.qualifications`,
    # `patient.user`, and `activities` so `_summarize_to_dict` can populate
    # the joined display fields without N+1 queries.
    data = [
        AppointmentSummaryResponse.model_validate(
            appointments_service._summarize_to_dict(r)
        )
        for r in rows
    ]
    return build_offset_response(data, total=total, page=page, page_size=page_size)


@router.get(
    "/{appointment_id}",
    response_model=AppointmentResponse,
)
async def get_appointment_endpoint(
    appointment_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> AppointmentResponse:
    """Fetch a single appointment (without activities)."""
    agency_id = _require_agency(ctx)
    appt = await appointments_service.get_appointment(
        session,
        appointment_id=appointment_id,
        agency_id=agency_id,
        with_activities=False,
        with_patient=True,
        with_location=True,
    )
    _ensure_can_view(ctx, appt.patient.user_id)
    return _to_response(appt, with_items=False)


@router.get(
    "/{appointment_id}/with-items",
    response_model=AppointmentResponse,
)
async def get_appointment_with_items_endpoint(
    appointment_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> AppointmentResponse:
    """Fetch a single appointment eagerly loaded with its activities
    and the patient-or-guardian signature from the visit (if signed)."""
    agency_id = _require_agency(ctx)
    appt = await appointments_service.get_appointment(
        session,
        appointment_id=appointment_id,
        agency_id=agency_id,
        with_activities=True,
        with_patient=True,
        with_signature=True,
        with_location=True,
    )
    _ensure_can_view(ctx, appt.patient.user_id)
    return _to_response(appt, with_items=True)


@router.patch(
    "/{appointment_id}",
    response_model=AppointmentResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def update_appointment_endpoint(
    appointment_id: uuid.UUID,
    payload: AppointmentUpdateRequest,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> AppointmentResponse:
    """Patch window / staff / program / location / notes."""
    agency_id = _require_agency(ctx)
    appt = await appointments_service.update_appointment(
        session, appointment_id=appointment_id, agency_id=agency_id, payload=payload
    )
    await session.commit()
    await session.refresh(appt)
    # Best-effort audit log.
    try:
        ip, ua = audit_logs_service.request_ip_ua(request)
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.UPDATE,
            entity_type="APPOINTMENT",
            entity_id=appt.id,
            new_data=payload.model_dump(mode="json"),
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    return _to_response(appt, with_items=False)


@router.post(
    "/{appointment_id}/cancel",
    response_model=AppointmentResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def cancel_appointment_endpoint(
    appointment_id: uuid.UUID,
    payload: AppointmentCancelRequest,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> AppointmentResponse:
    """Cancel an appointment (pre-visit only). Idempotent."""
    agency_id = _require_agency(ctx)
    ip, ua = audit_logs_service.request_ip_ua(request)
    appt = await appointments_service.cancel_appointment(
        session,
        appointment_id=appointment_id,
        agency_id=agency_id,
        payload=payload,
        actor_user_id=ctx.user_id,
    )
    await session.commit()
    await session.refresh(appt)
    # Best-effort audit log.
    try:
        ip, ua = audit_logs_service.request_ip_ua(request)
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.APPOINTMENT_CANCELLED,
            entity_type="APPOINTMENT",
            entity_id=appt.id,
            new_data={"reason": payload.reason},
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    return _to_response(appt, with_items=False)


@router.post(
    "/{appointment_id}/transition",
    response_model=AppointmentResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def transition_status_endpoint(
    appointment_id: uuid.UUID,
    payload: AppointmentStatusTransitionRequest,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> AppointmentResponse:
    """Walk the appointment through the lifecycle state machine.

    Validates the (current → requested) edge exists; otherwise 409.
    Drives the spec-aligned 5-state lifecycle. Visit-side transitions
    (IN_PROGRESS → AWAITING_SIGNATURE → COMPLETED) are handled by the
    visits module, gated by activities + billing + signature.
    """
    agency_id = _require_agency(ctx)
    ip, ua = audit_logs_service.request_ip_ua(request)
    pre_status = (
        await appointments_service.get_appointment(
            session,
            appointment_id=appointment_id,
            agency_id=agency_id,
        )
    ).status
    appt = await appointments_service.transition_status(
        session,
        appointment_id=appointment_id,
        agency_id=agency_id,
        payload=payload,
        actor_user_id=ctx.user_id,
    )
    await session.commit()
    await session.refresh(appt)
    # Best-effort audit log.
    try:
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.STATUS_TRANSITION,
            entity_type="APPOINTMENT",
            entity_id=appt.id,
            new_data={
                "from_status": pre_status.value
                if hasattr(pre_status, "value")
                else str(pre_status),
                "to_status": payload.status.value
                if hasattr(payload.status, "value")
                else str(payload.status),
                "note": payload.note,
            },
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    return _to_response(appt, with_items=False)


@router.post(
    "/{appointment_id}/ready",
    response_model=AppointmentResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def mark_appointment_ready_endpoint(
    appointment_id: uuid.UUID,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> AppointmentResponse:
    """Admin marks an appointment as READY for the caregiver.

    Transitions `SCHEDULED → READY`. The caregiver is then expected to
    call `POST /visits` to actually start the visit (which transitions
    `READY → IN_PROGRESS` on the visit row, and the appointment follows).
    """
    agency_id = _require_agency(ctx)
    ip, ua = audit_logs_service.request_ip_ua(request)
    appt = await appointments_service.mark_appointment_ready(
        session,
        appointment_id=appointment_id,
        agency_id=agency_id,
        actor_user_id=ctx.user_id,
    )
    await session.commit()
    await session.refresh(appt)
    # Best-effort audit log.
    try:
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.APPOINTMENT_MARKED_READY,
            entity_type="APPOINTMENT",
            entity_id=appt.id,
            new_data={"to_status": appt.status.value},
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    return _to_response(appt, with_items=False)


@router.post(
    "/{appointment_id}/assign",
    response_model=AppointmentResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def assign_staff_endpoint(
    appointment_id: uuid.UUID,
    staff_id: uuid.UUID,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> AppointmentResponse:
    """Assign (or re-assign) the staff member who will perform the visit."""
    agency_id = _require_agency(ctx)
    ip, ua = audit_logs_service.request_ip_ua(request)
    appt = await appointments_service.assign_staff(
        session,
        appointment_id=appointment_id,
        agency_id=agency_id,
        staff_id=staff_id,
    )
    await session.commit()
    await session.refresh(appt)
    # Best-effort audit log.
    try:
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.APPOINTMENT_ASSIGNED,
            entity_type="APPOINTMENT",
            entity_id=appt.id,
            new_data={"staff_id": str(staff_id)},
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    return _to_response(appt, with_items=False)


@router.post(
    "/{appointment_id}/missed",
    response_model=AppointmentResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def mark_appointment_missed_endpoint(
    appointment_id: uuid.UUID,
    payload: AppointmentMissedRequest,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> AppointmentResponse:
    """Mark an appointment as MISSED (caregiver no-show / patient unavailable).

    Allowed from `SCHEDULED` or `READY`. After a visit has started
    (`IN_PROGRESS`) the visit-side transition should be used instead.
    """
    agency_id = _require_agency(ctx)
    ip, ua = audit_logs_service.request_ip_ua(request)
    appt = await appointments_service.mark_appointment_missed(
        session,
        appointment_id=appointment_id,
        agency_id=agency_id,
        payload=payload,
        actor_user_id=ctx.user_id,
    )
    await session.commit()
    await session.refresh(appt)
    try:
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.STATUS_TRANSITION,
            entity_type="APPOINTMENT",
            entity_id=appt.id,
            new_data={
                "from_status": "PRE_VISIT",
                "to_status": appt.status.value,
                "reason": payload.reason,
                "transition": "missed",
            },
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    return _to_response(appt, with_items=False)


@router.post(
    "/{appointment_id}/rejected",
    response_model=AppointmentResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def mark_appointment_rejected_endpoint(
    appointment_id: uuid.UUID,
    payload: AppointmentRejectedRequest,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> AppointmentResponse:
    """Mark an appointment as REJECTED (patient/guardian declined).

    Allowed only from `SCHEDULED`. After the visit has started the
    patient disagreement is surfaced via the signature / dispute flow,
    not REJECTED.
    """
    agency_id = _require_agency(ctx)
    ip, ua = audit_logs_service.request_ip_ua(request)
    appt = await appointments_service.mark_appointment_rejected(
        session,
        appointment_id=appointment_id,
        agency_id=agency_id,
        payload=payload,
        actor_user_id=ctx.user_id,
    )
    await session.commit()
    await session.refresh(appt)
    try:
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.STATUS_TRANSITION,
            entity_type="APPOINTMENT",
            entity_id=appt.id,
            new_data={
                "from_status": "SCHEDULED",
                "to_status": appt.status.value,
                "reason": payload.reason,
                "transition": "rejected",
            },
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    return _to_response(appt, with_items=False)


@router.post(
    "/{appointment_id}/billing/paid",
    response_model=AppointmentResponse,
    dependencies=[
        Depends(require_role(UserRole.AGENCY_ADMIN, UserRole.STAFF))
    ],
)
async def mark_appointment_billing_paid_endpoint(
    appointment_id: uuid.UUID,
    payload: AppointmentMarkBillingPaidRequest,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> AppointmentResponse:
    """Flip the billing toggle to `paid` (idempotent).

    The visit-side `billing_confirmed_at` is the caregiver's
    clinical sign-off; this endpoint flips the *payment* flag the
    agency-admin / staff toggles after payment is processed.
    """
    agency_id = _require_agency(ctx)
    ip, ua = audit_logs_service.request_ip_ua(request)
    appt = await appointments_service.mark_appointment_billing_paid(
        session,
        appointment_id=appointment_id,
        agency_id=agency_id,
        by_user_id=ctx.user_id,
    )
    await session.commit()
    await session.refresh(appt)
    try:
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.BILLING_CONFIRMED,
            entity_type="APPOINTMENT",
            entity_id=appt.id,
            new_data={
                "billing_status": appt.billing_status,
                "billing_paid_at": appt.billing_paid_at.isoformat()
                if appt.billing_paid_at
                else None,
                "claim_id": appt.claim_id,
            },
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    return _to_response(appt, with_items=False)


# --------------------------------------------------------------------------
# Activities
# --------------------------------------------------------------------------
@router.get(
    "/{appointment_id}/activities",
    response_model=list[AppointmentActivityResponse],
)
async def list_activities_endpoint(
    appointment_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> list[AppointmentActivityResponse]:
    """List the activities under an appointment (oldest first)."""
    agency_id = _require_agency(ctx)
    appt = await appointments_service.get_appointment(
        session,
        appointment_id=appointment_id,
        agency_id=agency_id,
        with_activities=False,
        with_patient=True,
    )
    _ensure_can_view(ctx, appt.patient.user_id)
    items = await appointments_service.list_activities(
        session, appointment_id=appointment_id, agency_id=agency_id
    )
    return [AppointmentActivityResponse.model_validate(i) for i in items]


@router.post(
    "/{appointment_id}/activities",
    response_model=AppointmentActivityResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def add_activity_endpoint(
    appointment_id: uuid.UUID,
    payload: AppointmentActivityCreateRequest,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> AppointmentActivityResponse:
    """Add a free-text activity to an existing (non-finalized) appointment.

    Per spec §2, activities are free-text names entered by the admin at
    scheduling time ("Check blood pressure", "Prepare meal", etc.).
    """
    agency_id = _require_agency(ctx)
    item = await appointments_service.add_activity(
        session, appointment_id=appointment_id, agency_id=agency_id, payload=payload
    )
    await session.commit()
    await session.refresh(item)
    # Best-effort audit log.
    try:
        ip, ua = audit_logs_service.request_ip_ua(request)
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.CREATE,
            entity_type="APPOINTMENT_ACTIVITY",
            entity_id=item.id,
            new_data={
                "appointment_id": str(appointment_id),
                "name": payload.name,
                "planned_minutes": payload.planned_minutes,
            },
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    return AppointmentActivityResponse.model_validate(item)


@router.patch(
    "/{appointment_id}/activities/{activity_id}",
    response_model=AppointmentActivityResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def update_activity_endpoint(
    appointment_id: uuid.UUID,
    activity_id: uuid.UUID,
    payload: AppointmentActivityUpdateRequest,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> AppointmentActivityResponse:
    """Patch an activity's name / planned minutes / status / notes."""
    agency_id = _require_agency(ctx)
    item = await appointments_service.update_activity(
        session,
        activity_id=activity_id,
        appointment_id=appointment_id,
        agency_id=agency_id,
        payload=payload,
    )
    await session.commit()
    await session.refresh(item)
    # Best-effort audit log.
    try:
        ip, ua = audit_logs_service.request_ip_ua(request)
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.UPDATE,
            entity_type="APPOINTMENT_ACTIVITY",
            entity_id=item.id,
            new_data={
                "appointment_id": str(appointment_id),
                **payload.model_dump(mode="json"),
            },
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    return AppointmentActivityResponse.model_validate(item)


@router.delete(
    "/{appointment_id}/activities/{activity_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def delete_activity_endpoint(
    appointment_id: uuid.UUID,
    activity_id: uuid.UUID,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> Response:
    """Remove a PENDING activity. Non-pending activities cannot be deleted."""
    agency_id = _require_agency(ctx)
    await appointments_service.delete_activity(
        session,
        activity_id=activity_id,
        appointment_id=appointment_id,
        agency_id=agency_id,
    )
    await session.commit()
    # Best-effort audit log.
    try:
        ip, ua = audit_logs_service.request_ip_ua(request)
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.DELETE,
            entity_type="APPOINTMENT_ACTIVITY",
            entity_id=activity_id,
            new_data={"appointment_id": str(appointment_id)},
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------
# Self-service `/me/appointments/*` routes — date-bucketed, no pagination.
#
# The `/appointments?page=&page_size=` endpoint is paginated for admin
# tooling but the FE calendar UI wants date-bucketed, role-scoped
# responses ("today / upcoming / past"). These endpoints:
#   - resolve the caller's patient / staff / guardian linkage from
#     `ctx.user_id` server-side (so the URL doesn't leak identity),
#   - take an inclusive `date_from` / `date_to` calendar window,
#   - return either a flat list (`/calendar`) or a bucketed shape
#     (`/calendar/grouped`) — the FE picks whichever it needs.
#
# `AGENCY_ADMIN` and `SUPER_ADMIN` keep using `/appointments` for
# cross-user lookups.
# --------------------------------------------------------------------------
me_router = APIRouter(prefix="/me/appointments", tags=["appointments"])


def _today_utc() -> date:
    """Calendar today in UTC.

    The FE renders the "Today" bucket against the user's local day,
    but the bucket boundaries are computed server-side from the
    stored timestamps (which are stored in UTC). Treating "today" as
    `utc_now().date()` keeps the bucket stable across timezones; if
    the FE needs a per-user timezone it can split buckets client-side
    using `scheduled_start`.
    """
    from src.shared.utils.datetime_utils import utc_now

    return utc_now().date()


def _bucket(
    rows: Sequence[Appointment],
) -> tuple[list, list, list]:
    """Partition a flat list of appointments into (today, upcoming, past).

    Each item is projected to `AppointmentSummaryResponse` via the
    shared `_summarize_to_dict` helper so the wire shape matches
    everything else the FE already renders.
    """
    today = _today_utc()
    today_list: list = []
    upcoming_list: list = []
    past_list: list = []
    for appt in rows:
        start = getattr(appt, "scheduled_start", None)
        summary = AppointmentSummaryResponse.model_validate(
            appointments_service._summarize_to_dict(appt)
        )
        if start is None:
            # Defensive — every appointment has a scheduled_start but
            # if we ever accept "unscheduled" rows we still render them
            # in the upcoming bucket so they're visible.
            upcoming_list.append(summary)
            continue
        if start.date() == today:
            today_list.append(summary)
        elif start.date() > today:
            upcoming_list.append(summary)
        else:
            past_list.append(summary)
    return today_list, upcoming_list, past_list


@me_router.get(
    "/calendar",
    response_model=list[AppointmentSummaryResponse],
    responses=standard_responses(include=[401, 403, 404]),
    summary="Caller's appointments in a date window (no pagination)",
    description=(
        "Self-service. Returns every appointment for the caller "
        "within `[date_from, date_to]` (both inclusive, calendar "
        "dates in UTC), sorted ascending by `scheduled_start`. "
        "No pagination — the FE calendar typically wants every "
        "appointment in a 1-3 month window.\n\n"
        "Role scope:\n"
        "  - **PATIENT** — appointments where `patient_id == caller`\n"
        "  - **STAFF**   — appointments where `staff_id == caller's staff profile`\n"
        "  - **GUARDIAN**— appointments for every patient the caller is currently linked to\n\n"
        "Defaults to a ±30 day window around today if no dates are given."
    ),
    dependencies=[
        Depends(
            require_role(
                UserRole.PATIENT, UserRole.STAFF, UserRole.GUARDIAN
            )
        )
    ],
)
async def my_calendar_endpoint(
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    date_from: date | None = Query(
        default=None,
        description="Inclusive start date (YYYY-MM-DD). Default: today - 30 days.",
    ),
    date_to: date | None = Query(
        default=None,
        description="Inclusive end date (YYYY-MM-DD). Default: today + 60 days.",
    ),
    status_filter: AppointmentStatus | None = Query(
        default=None, alias="status"
    ),
) -> list[AppointmentSummaryResponse]:
    from src.modules.patients import service as patients_service

    agency_id = _require_agency(ctx)
    today = _today_utc()
    effective_from = date_from or (today - timedelta(days=30))
    effective_to = date_to or (today + timedelta(days=60))
    if effective_to < effective_from:
        raise ValidationError(
            "date_to must be on or after date_from.",
            details={
                "date_from": effective_from.isoformat(),
                "date_to": effective_to.isoformat(),
            },
        )

    patient_id: uuid.UUID | None = None
    staff_id: uuid.UUID | None = None

    if ctx.role == UserRole.PATIENT:
        patient = await patients_service.get_patient_by_user_id(
            session, user_id=ctx.user_id, agency_id=agency_id
        )
        patient_id = patient.id
    elif ctx.role == UserRole.STAFF:
        from src.modules.staff import service as staff_service

        staff = await staff_service.get_staff_by_user_id(
            session, user_id=ctx.user_id, agency_id=agency_id
        )
        staff_id = staff.id
    elif ctx.role == UserRole.GUARDIAN:
        guardian = await patients_service.get_guardian_by_user_id(
            session, user_id=ctx.user_id, agency_id=agency_id
        )
        patient_ids = await patients_service.list_guardian_patient_ids(
            session, guardian_id=guardian.id, agency_id=agency_id
        )
        if not patient_ids:
            return []
        # `list_appointments_in_window` only takes one patient_id, so
        # fan out per-patient and merge. For a typical guardian with
        # 1-3 linked patients the cost is fine; the alternative would
        # be a `patient_ids` filter on the service layer.
        merged: list[AppointmentSummaryResponse] = []
        for pid in patient_ids:
            rows = await appointments_service.list_appointments_in_window(
                session,
                agency_id=agency_id,
                date_from=effective_from,
                date_to=effective_to,
                patient_id=pid,
                status_filter=status_filter,
                include_past=True,
            )
            merged.extend(
                AppointmentSummaryResponse.model_validate(
                    appointments_service._summarize_to_dict(r)
                )
                for r in rows
            )
        merged.sort(key=lambda a: a.scheduled_start)
        return merged

    rows = await appointments_service.list_appointments_in_window(
        session,
        agency_id=agency_id,
        date_from=effective_from,
        date_to=effective_to,
        patient_id=patient_id,
        staff_id=staff_id,
        status_filter=status_filter,
        include_past=True,
    )
    return [
        AppointmentSummaryResponse.model_validate(
            appointments_service._summarize_to_dict(r)
        )
        for r in rows
    ]


@me_router.get(
    "/calendar/grouped",
    response_model=CalendarAppointmentsResponse,
    responses=standard_responses(include=[401, 403, 404]),
    summary="Caller's appointments bucketed (today / upcoming / past)",
    description=(
        "Self-service. Same data as `/me/appointments/calendar` but "
        "partitioned into three buckets so the FE calendar can render "
        "today / upcoming / past sections directly without re-bucketing "
        "client-side. Bucket boundaries are computed in UTC."
    ),
    dependencies=[
        Depends(
            require_role(
                UserRole.PATIENT, UserRole.STAFF, UserRole.GUARDIAN
            )
        )
    ],
)
async def my_calendar_grouped_endpoint(
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    date_from: date | None = Query(
        default=None,
        description="Inclusive start date (YYYY-MM-DD). Default: today - 30 days.",
    ),
    date_to: date | None = Query(
        default=None,
        description="Inclusive end date (YYYY-MM-DD). Default: today + 60 days.",
    ),
    status_filter: AppointmentStatus | None = Query(
        default=None, alias="status"
    ),
) -> CalendarAppointmentsResponse:
    # Reuse the flat endpoint so role scoping + filtering stays in
    # one place; just partition its output into buckets here.
    flat = await my_calendar_endpoint(
        ctx=ctx,
        session=session,
        date_from=date_from,
        date_to=date_to,
        status_filter=status_filter,
    )
    today = _today_utc()
    today_list: list[AppointmentSummaryResponse] = []
    upcoming_list: list[AppointmentSummaryResponse] = []
    past_list: list[AppointmentSummaryResponse] = []
    for appt in flat:
        if appt.scheduled_start is None:
            upcoming_list.append(appt)
            continue
        d = appt.scheduled_start.date()
        if d == today:
            today_list.append(appt)
        elif d > today:
            upcoming_list.append(appt)
        else:
            past_list.append(appt)
    return CalendarAppointmentsResponse(
        today=today_list,
        upcoming=upcoming_list,
        past=past_list,
    )


__all__ = ["router", "me_router"]