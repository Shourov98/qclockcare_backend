"""Staff router — `/staff` and `/staff/{id}/...` endpoints.

All routes require authentication. Roster-mutating routes (create, update,
archive, qualification + availability writes) require AGENCY_ADMIN at
the same agency. STAFF users can read their own profile + their own
qualifications + their own availability (RLS enforces this — the router
just passes the auth context through).

Endpoints:
  POST   /staff                                       — invite staff
  GET    /staff                                       — list (paginated)
  GET    /staff/{id}                                  — fetch (summary)
  GET    /staff/{id}/with-details                     — fetch + qual + avail
  PATCH  /staff/{id}                                  — update
  DELETE /staff/{id}                                  — archive

  GET    /staff/{id}/qualifications
  POST   /staff/{id}/qualifications
  PATCH  /staff/{id}/qualifications/{qid}
  DELETE /staff/{id}/qualifications/{qid}             — revoke

  GET    /staff/{id}/availability
  POST   /staff/{id}/availability
  PATCH  /staff/{id}/availability/{aid}
  DELETE /staff/{id}/availability/{aid}
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import CrossAgencyAccessDeniedError, ForbiddenError
from src.core.logging import get_logger
from src.modules.audit_logs import service as audit_logs_service
from src.modules.identity.dependencies import (
    CurrentAuth,
    get_session_with_auth,
    require_role,
)
from src.modules.staff import service as staff_service
from src.modules.staff.schemas import (
    StaffAvailabilityCreateRequest,
    StaffAvailabilityResponse,
    StaffAvailabilityUpdateRequest,
    StaffProfileCreateRequest,
    StaffProfileResponse,
    StaffProfileSummaryResponse,
    StaffProfileUpdateRequest,
    StaffQualificationCreateRequest,
    StaffQualificationResponse,
    StaffQualificationUpdateRequest,
)
from src.shared.domain.enums import AuditAction, UserRole, UserStatus
from src.shared.schemas.pagination import (
    PaginatedResponse,
    build_offset_response,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/staff", tags=["staff"])


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _require_agency(ctx: CurrentAuth) -> uuid.UUID:
    """AGENCY_ADMIN / STAFF must have an agency; SUPER_ADMIN is rejected here."""
    if ctx.role == UserRole.SUPER_ADMIN:
        raise ForbiddenError(
            "Use the platform admin console for cross-agency staff operations."
        )
    if ctx.agency_id is None:
        raise ForbiddenError("Caller has no agency context.")
    return ctx.agency_id


def _ensure_can_view(ctx: CurrentAuth, staff_user_id: uuid.UUID) -> None:
    """Either AGENCY_ADMIN at the agency, or the staff member themselves."""
    if ctx.role == UserRole.SUPER_ADMIN:
        return
    if ctx.role == UserRole.AGENCY_ADMIN:
        return
    # STAFF / PATIENT etc. can only see their own profile.
    if ctx.user_id != staff_user_id:
        raise CrossAgencyAccessDeniedError()


def _to_response(
    staff: object,
    *,
    with_details: bool = False,
) -> StaffProfileResponse:
    """Build a StaffProfileResponse without triggering lazy loads.

    `StaffProfile.qualifications` and `.availability` are lazy-loaded
    relationships — calling `model_validate(staff)` would trigger async
    IO outside an awaited context. We build the dict explicitly and only
    include the nested children when explicitly requested (i.e. when
    `with_details=True` and the collections are already loaded).
    """
    # `from_attributes=True` lets us build a dict-like input from the ORM row.
    data: dict = {
        "id": staff.id,
        "agency_id": staff.agency_id,
        "user_id": staff.user_id,
        "staff_code": staff.staff_code,
        "status": staff.status,
        "hired_at": staff.hired_at,
        "terminated_at": staff.terminated_at,
        "created_at": staff.created_at,
        "updated_at": staff.updated_at,
    }
    if with_details:
        # The collections were eager-loaded by the service; safe to read.
        try:
            data["qualifications"] = list(staff.qualifications)
        except Exception:
            data["qualifications"] = None
        try:
            data["availability"] = list(staff.availability)
        except Exception:
            data["availability"] = None
    else:
        data["qualifications"] = None
        data["availability"] = None
    return StaffProfileResponse.model_validate(data)


# --------------------------------------------------------------------------
# Staff profiles
# --------------------------------------------------------------------------
@router.post(
    "",
    response_model=StaffProfileResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def create_staff_endpoint(
    payload: StaffProfileCreateRequest,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> StaffProfileResponse:
    """Invite a new staff member at the caller's agency."""
    agency_id = _require_agency(ctx)
    profile = await staff_service.create_staff(
        session,
        agency_id=agency_id,
        payload=payload,
        invited_by_user_id=ctx.user_id,
    )
    await session.commit()
    await session.refresh(profile)
    # Best-effort audit log.
    try:
        ip, ua = audit_logs_service.request_ip_ua(request)
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.CREATE,
            entity_type="STAFF_PROFILE",
            entity_id=profile.id,
            new_data={
                "staff_code": profile.staff_code,
                "user_id": str(profile.user_id),
            },
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    return _to_response(profile, with_details=False)


@router.get(
    "",
    response_model=PaginatedResponse[StaffProfileSummaryResponse],
)
async def list_staff_endpoint(
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    status_filter: UserStatus | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1, le=10000),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    """List staff at the caller's agency (paginated)."""
    agency_id = _require_agency(ctx)
    rows, total = await staff_service.list_staff(
        session,
        agency_id=agency_id,
        status_filter=status_filter,
        page=page,
        page_size=page_size,
    )
    data = [StaffProfileSummaryResponse.model_validate(r) for r in rows]
    return build_offset_response(
        data,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/{staff_id}",
    response_model=StaffProfileResponse,
)
async def get_staff_endpoint(
    staff_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> StaffProfileResponse:
    """Fetch a single staff profile (summary)."""
    agency_id = _require_agency(ctx)
    staff = await staff_service.get_staff(
        session, staff_id=staff_id, agency_id=agency_id, with_details=False
    )
    _ensure_can_view(ctx, staff.user_id)
    return _to_response(staff, with_details=False)


@router.get(
    "/{staff_id}/with-details",
    response_model=StaffProfileResponse,
)
async def get_staff_with_details_endpoint(
    staff_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> StaffProfileResponse:
    """Fetch a staff profile + nested qualifications + availability."""
    agency_id = _require_agency(ctx)
    staff = await staff_service.get_staff(
        session, staff_id=staff_id, agency_id=agency_id, with_details=True
    )
    _ensure_can_view(ctx, staff.user_id)
    return _to_response(staff, with_details=True)


@router.patch(
    "/{staff_id}",
    response_model=StaffProfileResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def update_staff_endpoint(
    staff_id: uuid.UUID,
    payload: StaffProfileUpdateRequest,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> StaffProfileResponse:
    agency_id = _require_agency(ctx)
    staff = await staff_service.update_staff(
        session, staff_id=staff_id, agency_id=agency_id, payload=payload
    )
    await session.commit()
    await session.refresh(staff)
    # Best-effort audit log.
    try:
        ip, ua = audit_logs_service.request_ip_ua(request)
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.UPDATE,
            entity_type="STAFF_PROFILE",
            entity_id=staff.id,
            new_data=payload.model_dump(mode="json"),
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    return _to_response(staff, with_details=False)


@router.delete(
    "/{staff_id}",
    response_model=StaffProfileResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def archive_staff_endpoint(
    staff_id: uuid.UUID,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> StaffProfileResponse:
    agency_id = _require_agency(ctx)
    staff = await staff_service.archive_staff(
        session, staff_id=staff_id, agency_id=agency_id
    )
    await session.commit()
    await session.refresh(staff)
    # Best-effort audit log.
    try:
        ip, ua = audit_logs_service.request_ip_ua(request)
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.DELETE,
            entity_type="STAFF_PROFILE",
            entity_id=staff.id,
            new_data={"status": staff.status.value if hasattr(staff.status, "value") else str(staff.status)},
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    return _to_response(staff, with_details=False)


# --------------------------------------------------------------------------
# Qualifications
# --------------------------------------------------------------------------
@router.get(
    "/{staff_id}/qualifications",
    response_model=list[StaffQualificationResponse],
)
async def list_qualifications_endpoint(
    staff_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> list[StaffQualificationResponse]:
    agency_id = _require_agency(ctx)
    staff = await staff_service.get_staff(
        session, staff_id=staff_id, agency_id=agency_id
    )
    _ensure_can_view(ctx, staff.user_id)
    quals = await staff_service.list_qualifications(
        session, staff_id=staff_id, agency_id=agency_id
    )
    return [StaffQualificationResponse.model_validate(q) for q in quals]


@router.post(
    "/{staff_id}/qualifications",
    response_model=StaffQualificationResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def add_qualification_endpoint(
    staff_id: uuid.UUID,
    payload: StaffQualificationCreateRequest,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> StaffQualificationResponse:
    agency_id = _require_agency(ctx)
    qual = await staff_service.add_qualification(
        session, staff_id=staff_id, agency_id=agency_id, payload=payload
    )
    await session.commit()
    await session.refresh(qual)
    # Best-effort audit log.
    try:
        ip, ua = audit_logs_service.request_ip_ua(request)
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.CREATE,
            entity_type="STAFF_QUALIFICATION",
            entity_id=qual.id,
            new_data={
                "staff_id": str(staff_id),
                **payload.model_dump(mode="json"),
            },
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    return StaffQualificationResponse.model_validate(qual)


@router.patch(
    "/{staff_id}/qualifications/{qualification_id}",
    response_model=StaffQualificationResponse,
)
async def update_qualification_endpoint(
    staff_id: uuid.UUID,
    qualification_id: uuid.UUID,
    payload: StaffQualificationUpdateRequest,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> StaffQualificationResponse:
    """Update a qualification. AGENCY_ADMIN or the staff member themselves."""
    agency_id = _require_agency(ctx)
    # RLS already scopes the read; we add a defence-in-depth auth check.
    if ctx.role == UserRole.STAFF:
        staff = await staff_service.get_staff(
            session, staff_id=staff_id, agency_id=agency_id
        )
        if staff.user_id != ctx.user_id:
            raise ForbiddenError("Staff can only update their own qualifications.")
    elif ctx.role != UserRole.AGENCY_ADMIN:
        raise ForbiddenError("Only AGENCY_ADMIN or the staff member may edit.")

    qual = await staff_service.update_qualification(
        session,
        qualification_id=qualification_id,
        staff_id=staff_id,
        agency_id=agency_id,
        payload=payload,
    )
    await session.commit()
    await session.refresh(qual)
    # Best-effort audit log.
    try:
        ip, ua = audit_logs_service.request_ip_ua(request)
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.UPDATE,
            entity_type="STAFF_QUALIFICATION",
            entity_id=qual.id,
            new_data={"staff_id": str(staff_id), **payload.model_dump(mode="json")},
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    return StaffQualificationResponse.model_validate(qual)


@router.delete(
    "/{staff_id}/qualifications/{qualification_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def revoke_qualification_endpoint(
    staff_id: uuid.UUID,
    qualification_id: uuid.UUID,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> Response:
    agency_id = _require_agency(ctx)
    await staff_service.revoke_qualification(
        session,
        qualification_id=qualification_id,
        staff_id=staff_id,
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
            entity_type="STAFF_QUALIFICATION",
            entity_id=qualification_id,
            new_data={"staff_id": str(staff_id)},
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------
@router.get(
    "/{staff_id}/availability",
    response_model=list[StaffAvailabilityResponse],
)
async def list_availability_endpoint(
    staff_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> list[StaffAvailabilityResponse]:
    agency_id = _require_agency(ctx)
    staff = await staff_service.get_staff(
        session, staff_id=staff_id, agency_id=agency_id
    )
    _ensure_can_view(ctx, staff.user_id)
    rows = await staff_service.list_availability(
        session, staff_id=staff_id, agency_id=agency_id
    )
    return [StaffAvailabilityResponse.model_validate(r) for r in rows]


@router.post(
    "/{staff_id}/availability",
    response_model=StaffAvailabilityResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_availability_endpoint(
    staff_id: uuid.UUID,
    payload: StaffAvailabilityCreateRequest,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> StaffAvailabilityResponse:
    """Add an availability row. AGENCY_ADMIN or the staff member themselves."""
    agency_id = _require_agency(ctx)
    if ctx.role == UserRole.STAFF:
        staff = await staff_service.get_staff(
            session, staff_id=staff_id, agency_id=agency_id
        )
        if staff.user_id != ctx.user_id:
            raise ForbiddenError("Staff can only edit their own availability.")
    elif ctx.role != UserRole.AGENCY_ADMIN:
        raise ForbiddenError("Only AGENCY_ADMIN or the staff member may edit.")

    avail = await staff_service.add_availability(
        session, staff_id=staff_id, agency_id=agency_id, payload=payload
    )
    await session.commit()
    await session.refresh(avail)
    # Best-effort audit log.
    try:
        ip, ua = audit_logs_service.request_ip_ua(request)
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.CREATE,
            entity_type="STAFF_AVAILABILITY",
            entity_id=avail.id,
            new_data={"staff_id": str(staff_id), **payload.model_dump(mode="json")},
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    return StaffAvailabilityResponse.model_validate(avail)


@router.patch(
    "/{staff_id}/availability/{availability_id}",
    response_model=StaffAvailabilityResponse,
)
async def update_availability_endpoint(
    staff_id: uuid.UUID,
    availability_id: uuid.UUID,
    payload: StaffAvailabilityUpdateRequest,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> StaffAvailabilityResponse:
    agency_id = _require_agency(ctx)
    if ctx.role == UserRole.STAFF:
        staff = await staff_service.get_staff(
            session, staff_id=staff_id, agency_id=agency_id
        )
        if staff.user_id != ctx.user_id:
            raise ForbiddenError("Staff can only edit their own availability.")
    elif ctx.role != UserRole.AGENCY_ADMIN:
        raise ForbiddenError("Only AGENCY_ADMIN or the staff member may edit.")

    avail = await staff_service.update_availability(
        session,
        availability_id=availability_id,
        staff_id=staff_id,
        agency_id=agency_id,
        payload=payload,
    )
    await session.commit()
    await session.refresh(avail)
    # Best-effort audit log.
    try:
        ip, ua = audit_logs_service.request_ip_ua(request)
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.UPDATE,
            entity_type="STAFF_AVAILABILITY",
            entity_id=avail.id,
            new_data={"staff_id": str(staff_id), **payload.model_dump(mode="json")},
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    return StaffAvailabilityResponse.model_validate(avail)


@router.delete(
    "/{staff_id}/availability/{availability_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_availability_endpoint(
    staff_id: uuid.UUID,
    availability_id: uuid.UUID,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> Response:
    agency_id = _require_agency(ctx)
    if ctx.role == UserRole.STAFF:
        staff = await staff_service.get_staff(
            session, staff_id=staff_id, agency_id=agency_id
        )
        if staff.user_id != ctx.user_id:
            raise ForbiddenError("Staff can only edit their own availability.")
    elif ctx.role != UserRole.AGENCY_ADMIN:
        raise ForbiddenError("Only AGENCY_ADMIN or the staff member may edit.")

    await staff_service.delete_availability(
        session,
        availability_id=availability_id,
        staff_id=staff_id,
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
            entity_type="STAFF_AVAILABILITY",
            entity_id=availability_id,
            new_data={"staff_id": str(staff_id)},
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
