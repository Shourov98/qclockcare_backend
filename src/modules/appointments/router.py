"""Appointments router — `/appointments` and `/appointments/{id}/...`.

All routes require authentication. State-mutating routes (create, update,
cancel, transition, assign, service-item writes) require AGENCY_ADMIN or
an authorised STAFF member at the agency. Read routes are open to
AGENCY_ADMIN, STAFF, the patient themselves, and authorised guardians.

Endpoints:
  POST   /appointments                                — schedule visit
  GET    /appointments                                — list (paginated, filterable)
  GET    /appointments/{id}                           — fetch (summary)
  GET    /appointments/{id}/with-items                — fetch + nested service items
  PATCH  /appointments/{id}                           — patch window / staff / notes
  POST   /appointments/{id}/cancel                    — cancel (pre-visit only)
  POST   /appointments/{id}/transition                — status transition (state machine)
  POST   /appointments/{id}/assign                    — assign staff

  GET    /appointments/{id}/service-items
  POST   /appointments/{id}/service-items
  PATCH  /appointments/{id}/service-items/{item_id}
  DELETE /appointments/{id}/service-items/{item_id}
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import CrossAgencyAccessDeniedError, ForbiddenError
from src.core.logging import get_logger
from src.modules.appointments import service as appointments_service
from src.modules.appointments.schemas import (
    AppointmentCancelRequest,
    AppointmentCreateRequest,
    AppointmentResponse,
    AppointmentServiceItemCreateRequest,
    AppointmentServiceItemResponse,
    AppointmentServiceItemUpdateRequest,
    AppointmentStatusTransitionRequest,
    AppointmentSummaryResponse,
    AppointmentUpdateRequest,
)
from src.modules.identity.dependencies import (
    CurrentAuth,
    get_session_with_auth,
    require_role,
)
from src.shared.domain.enums import AppointmentStatus, UserRole
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

    `Appointment.service_items` is a lazy-loaded relationship. We only
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
        "confirmation_status": appt.confirmation_status,
        "confirmed_at": appt.confirmed_at,
        "confirmation_note": appt.confirmation_note,
        "checked_in_at": appt.checked_in_at,
        "checked_out_at": appt.checked_out_at,
        "completed_at": appt.completed_at,
        "location": appt.location,
        "notes": appt.notes,
        "cancelled_reason": appt.cancelled_reason,
        "cancelled_at": appt.cancelled_at,
        "created_at": appt.created_at,
        "updated_at": appt.updated_at,
    }
    if with_items:
        try:
            data["service_items"] = list(appt.service_items)
        except Exception:
            data["service_items"] = None
    else:
        data["service_items"] = None
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
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> AppointmentResponse:
    """Schedule a new appointment at the caller's agency."""
    agency_id = _require_agency(ctx)
    appt = await appointments_service.create_appointment(
        session,
        agency_id=agency_id,
        payload=payload,
        scheduled_by_user_id=ctx.user_id,
    )
    await session.commit()
    await session.refresh(appt)
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
    data = [AppointmentSummaryResponse.model_validate(r) for r in rows]
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
    """Fetch a single appointment (without service items)."""
    agency_id = _require_agency(ctx)
    appt = await appointments_service.get_appointment(
        session, appointment_id=appointment_id, agency_id=agency_id, with_items=False
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
    """Fetch a single appointment eagerly loaded with its service items."""
    agency_id = _require_agency(ctx)
    appt = await appointments_service.get_appointment(
        session, appointment_id=appointment_id, agency_id=agency_id, with_items=True
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
    return _to_response(appt, with_items=False)


@router.post(
    "/{appointment_id}/cancel",
    response_model=AppointmentResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def cancel_appointment_endpoint(
    appointment_id: uuid.UUID,
    payload: AppointmentCancelRequest,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> AppointmentResponse:
    """Cancel an appointment (pre-visit only). Idempotent."""
    agency_id = _require_agency(ctx)
    appt = await appointments_service.cancel_appointment(
        session, appointment_id=appointment_id, agency_id=agency_id, payload=payload
    )
    await session.commit()
    await session.refresh(appt)
    return _to_response(appt, with_items=False)


@router.post(
    "/{appointment_id}/transition",
    response_model=AppointmentResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def transition_status_endpoint(
    appointment_id: uuid.UUID,
    payload: AppointmentStatusTransitionRequest,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> AppointmentResponse:
    """Walk the appointment through the lifecycle state machine.

    Validates the (current → requested) edge exists; otherwise 409.
    Stamps lifecycle timestamps (confirmed_at, checked_in_at, etc.) on
    relevant transitions.
    """
    agency_id = _require_agency(ctx)
    appt = await appointments_service.transition_status(
        session, appointment_id=appointment_id, agency_id=agency_id, payload=payload
    )
    await session.commit()
    await session.refresh(appt)
    return _to_response(appt, with_items=False)


@router.post(
    "/{appointment_id}/assign",
    response_model=AppointmentResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def assign_staff_endpoint(
    appointment_id: uuid.UUID,
    staff_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> AppointmentResponse:
    """Assign (or re-assign) the staff member who will perform the visit."""
    agency_id = _require_agency(ctx)
    appt = await appointments_service.assign_staff(
        session,
        appointment_id=appointment_id,
        agency_id=agency_id,
        staff_id=staff_id,
    )
    await session.commit()
    await session.refresh(appt)
    return _to_response(appt, with_items=False)


# --------------------------------------------------------------------------
# Service items
# --------------------------------------------------------------------------
@router.get(
    "/{appointment_id}/service-items",
    response_model=list[AppointmentServiceItemResponse],
)
async def list_service_items_endpoint(
    appointment_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> list[AppointmentServiceItemResponse]:
    """List the service items under an appointment."""
    agency_id = _require_agency(ctx)
    appt = await appointments_service.get_appointment(
        session, appointment_id=appointment_id, agency_id=agency_id, with_items=False
    )
    _ensure_can_view(ctx, appt.patient.user_id)
    items = await appointments_service.list_service_items(
        session, appointment_id=appointment_id, agency_id=agency_id
    )
    return [AppointmentServiceItemResponse.model_validate(i) for i in items]


@router.post(
    "/{appointment_id}/service-items",
    response_model=AppointmentServiceItemResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def add_service_item_endpoint(
    appointment_id: uuid.UUID,
    payload: AppointmentServiceItemCreateRequest,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> AppointmentServiceItemResponse:
    """Add a service item to an existing (non-finalized) appointment."""
    agency_id = _require_agency(ctx)
    item = await appointments_service.add_service_item(
        session, appointment_id=appointment_id, agency_id=agency_id, payload=payload
    )
    await session.commit()
    await session.refresh(item)
    return AppointmentServiceItemResponse.model_validate(item)


@router.patch(
    "/{appointment_id}/service-items/{item_id}",
    response_model=AppointmentServiceItemResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def update_service_item_endpoint(
    appointment_id: uuid.UUID,
    item_id: uuid.UUID,
    payload: AppointmentServiceItemUpdateRequest,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> AppointmentServiceItemResponse:
    """Patch a service item's type / minutes / status / notes."""
    agency_id = _require_agency(ctx)
    item = await appointments_service.update_service_item(
        session,
        item_id=item_id,
        appointment_id=appointment_id,
        agency_id=agency_id,
        payload=payload,
    )
    await session.commit()
    await session.refresh(item)
    return AppointmentServiceItemResponse.model_validate(item)


@router.delete(
    "/{appointment_id}/service-items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def delete_service_item_endpoint(
    appointment_id: uuid.UUID,
    item_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> Response:
    """Remove a PENDING service item. Non-pending items cannot be deleted."""
    agency_id = _require_agency(ctx)
    await appointments_service.delete_service_item(
        session,
        item_id=item_id,
        appointment_id=appointment_id,
        agency_id=agency_id,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
