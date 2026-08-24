"""Appointments router — `/appointments` and `/appointments/{id}/...`.

All routes require authentication. State-mutating routes (create, update,
cancel, transition, ready, activity writes) require AGENCY_ADMIN. Read
routes are open to AGENCY_ADMIN, STAFF, the patient themselves, and
authorised guardians (RLS narrows the rows).

Endpoints:
  POST   /appointments                                — schedule visit
  GET    /appointments                                — list (paginated, filterable)
  GET    /appointments/{id}                           — fetch (summary)
  GET    /appointments/{id}/with-items                — fetch + nested activities
  PATCH  /appointments/{id}                           — patch window / staff / notes
  POST   /appointments/{id}/cancel                    — cancel (pre-visit only)
  POST   /appointments/{id}/transition                — status transition (state machine)
  POST   /appointments/{id}/ready                     — admin marks READY (SCHEDULED→READY)
  POST   /appointments/{id}/assign                    — assign staff

  GET    /appointments/{id}/activities
  POST   /appointments/{id}/activities
  PATCH  /appointments/{id}/activities/{activity_id}
  DELETE /appointments/{id}/activities/{activity_id}
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import CrossAgencyAccessDeniedError, ForbiddenError
from src.core.logging import get_logger
from src.modules.appointments import service as appointments_service
from src.modules.appointments.schemas import (
    AppointmentActivityCreateRequest,
    AppointmentActivityResponse,
    AppointmentActivityUpdateRequest,
    AppointmentCancelRequest,
    AppointmentCreateRequest,
    AppointmentResponse,
    AppointmentStatusTransitionRequest,
    AppointmentSummaryResponse,
    AppointmentUpdateRequest,
)
from src.modules.audit_logs import service as audit_logs_service
from src.modules.identity.dependencies import (
    CurrentAuth,
    get_session_with_auth,
    require_role,
)
from src.shared.domain.enums import AppointmentStatus, AuditAction, UserRole
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
    """
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
    }
    if with_items:
        try:
            data["activities"] = list(appt.activities)
        except Exception:
            data["activities"] = None
    else:
        data["activities"] = None
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
    page: int = Query(default=1, ge=1, le=10000),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    """Paginated list of appointments at the caller's agency.

    Optional filters: patient_id, staff_id, status.
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
        page=page,
        page_size=page_size,
    )
    # `list_appointments` eager-loads `staff.user` and `activities`
    # so `_summarize_to_dict` can populate the joined display fields
    # (caregiver name, program label, etc.) without N+1 queries.
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
    """Fetch a single appointment eagerly loaded with its activities."""
    agency_id = _require_agency(ctx)
    appt = await appointments_service.get_appointment(
        session,
        appointment_id=appointment_id,
        agency_id=agency_id,
        with_activities=True,
        with_patient=True,
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


__all__ = ["router"]