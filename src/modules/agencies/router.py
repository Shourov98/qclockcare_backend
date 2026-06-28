"""Agencies router — `/agencies` endpoints.

All endpoints require SUPER_ADMIN. There is intentionally no
agency-scoped variant — an AGENCY_ADMIN does not manage agencies
through this surface; their agency is managed by the SUPER_ADMIN.

Endpoints:
  GET    /agencies                     — list all (paginated)
  POST   /agencies                     — create one
  GET    /agencies/{agency_id}         — fetch one
  PATCH  /agencies/{agency_id}         — partial update
  DELETE /agencies/{agency_id}         — soft delete
  GET    /agencies/{agency_id}/programs — list programs the agency offers
"""

from __future__ import annotations

import uuid
from builtins import type as _type
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.logging import get_logger
from src.modules.agencies import service as agencies_service
from src.modules.agencies.schemas import (
    AgencyCreateRequest,
    AgencyListResponse,
    AgencyProgramListResponse,
    AgencyProgramResponse,
    AgencyResponse,
    AgencyUpdateRequest,
)
from src.modules.audit_logs import service as audit_logs_service
from src.modules.identity.dependencies import (
    CurrentAuth,
    get_session_with_auth,
    require_role,
)
from src.shared.domain.enums import AuditAction, UserRole
from src.shared.schemas.docs import standard_responses
from src.shared.schemas.pagination import build_offset_response

log = get_logger(__name__)

router = APIRouter(prefix="/agencies", tags=["agencies"])

# All agencies routes require SUPER_ADMIN.
_SUPER_ADMIN_ONLY = [Depends(require_role(UserRole.SUPER_ADMIN))]


# --------------------------------------------------------------------------
# List + create
# --------------------------------------------------------------------------
@router.get(
    "",
    response_model=AgencyListResponse,
    responses=standard_responses(include=[401, 403]),
)
async def list_agencies_endpoint(
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    include_deleted: Annotated[bool, Query()] = False,
    status_filter: Annotated[
        str | None,
        Query(description="Narrow to one AgencyStatus (ACTIVE | TRIAL | SUSPENDED | CHURNED)"),
    ] = None,
) -> AgencyListResponse:
    """List all agencies (SUPER_ADMIN only, paginated)."""
    rows, total = await agencies_service.list_agencies(
        session,
        page=page,
        page_size=page_size,
        include_deleted=include_deleted,
        status_filter=status_filter,
    )
    data = [AgencyResponse.model_validate(r) for r in rows]
    body = build_offset_response(data, total=total, page=page, page_size=page_size)
    return AgencyListResponse.model_validate(body)


@router.post(
    "",
    response_model=AgencyResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=_SUPER_ADMIN_ONLY,
    responses=standard_responses(include=[401, 403, 409, 422]),
)
async def create_agency_endpoint(
    payload: AgencyCreateRequest,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> AgencyResponse:
    """Create a new agency (and optionally attach programs)."""
    agency = await agencies_service.create_agency(session, payload=payload)
    await session.flush()

    # Best-effort audit hook — never breaks the write.
    ip, ua = audit_logs_service.request_ip_ua(request)
    try:
        await audit_logs_service.audit_log(
            session,
            agency_id=None,  # agency-level audit, no agency context yet
            actor_user_id=ctx.user_id,
            action=AuditAction.CREATE,
            entity_type="AGENCY",
            entity_id=agency.id,
            new_data={
                "name": agency.name,
                "timezone": agency.timezone,
                "status": agency.status.value,
                "initial_program_codes": payload.initial_program_codes,
            },
            ip_address=ip,
            user_agent=ua,
        )
    except Exception as exc:
        log.warning(
            "agencies.create_audit_failed",
            error=_type(exc).__name__,
        )

    await session.commit()
    return AgencyResponse.model_validate(agency)


# --------------------------------------------------------------------------
# Single-row reads + writes
# --------------------------------------------------------------------------
@router.get(
    "/{agency_id}",
    response_model=AgencyResponse,
    dependencies=_SUPER_ADMIN_ONLY,
    responses=standard_responses(include=[401, 403, 404]),
)
async def get_agency_endpoint(
    agency_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    include_deleted: Annotated[bool, Query()] = False,
) -> AgencyResponse:
    """Fetch one agency by id."""
    agency = await agencies_service.get_agency(
        session,
        agency_id=agency_id,
    )
    if include_deleted:
        # Re-fetch with deleted-included so the response reflects state.
        from src.modules.agencies import service as svc

        agency = await svc._get_agency_or_404(session, agency_id=agency_id, include_deleted=True)
    return AgencyResponse.model_validate(agency)


@router.patch(
    "/{agency_id}",
    response_model=AgencyResponse,
    dependencies=_SUPER_ADMIN_ONLY,
    responses=standard_responses(include=[401, 403, 404, 409, 422]),
)
async def update_agency_endpoint(
    agency_id: uuid.UUID,
    payload: AgencyUpdateRequest,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> AgencyResponse:
    """Partial update of one agency.

    Status transitions:
      - Any → SUSPENDED: stamps `settings.suspended_at`
      - Any → CHURNED:   stamps `settings.churned_at`
      - SUSPENDED → ACTIVE/TRIAL: clears `suspended_at`, stamps
        `reactivated_at`
    """
    agency = await agencies_service.update_agency(
        session,
        agency_id=agency_id,
        payload=payload,
    )
    await session.flush()

    # Audit — log only the fields the caller changed.
    ip, ua = audit_logs_service.request_ip_ua(request)
    try:
        await audit_logs_service.audit_log(
            session,
            agency_id=None,
            actor_user_id=ctx.user_id,
            action=AuditAction.UPDATE,
            entity_type="AGENCY",
            entity_id=agency.id,
            new_data=payload.model_dump(exclude_unset=True),
            ip_address=ip,
            user_agent=ua,
        )
    except Exception as exc:
        log.warning(
            "agencies.update_audit_failed",
            error=_type(exc).__name__,
        )

    await session.commit()
    return AgencyResponse.model_validate(agency)


@router.delete(
    "/{agency_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=_SUPER_ADMIN_ONLY,
    responses=standard_responses(include=[401, 403, 404]),
)
async def delete_agency_endpoint(
    agency_id: uuid.UUID,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> Response:
    """Soft-delete an agency (preserves history for FK references)."""
    agency = await agencies_service.soft_delete_agency(
        session,
        agency_id=agency_id,
    )
    await session.flush()

    ip, ua = audit_logs_service.request_ip_ua(request)
    try:
        await audit_logs_service.audit_log(
            session,
            agency_id=None,
            actor_user_id=ctx.user_id,
            action=AuditAction.DELETE,
            entity_type="AGENCY",
            entity_id=agency.id,
            new_data={"soft_delete": True},
            ip_address=ip,
            user_agent=ua,
        )
    except Exception as exc:
        log.warning(
            "agencies.delete_audit_failed",
            error=_type(exc).__name__,
        )

    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------
# Programs sub-resource
# --------------------------------------------------------------------------
@router.get(
    "/{agency_id}/programs",
    response_model=AgencyProgramListResponse,
    dependencies=_SUPER_ADMIN_ONLY,
    responses=standard_responses(include=[401, 403, 404]),
)
async def list_agency_programs_endpoint(
    agency_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> AgencyProgramListResponse:
    """List the programs the agency offers (joined with program details)."""
    rows = await agencies_service.list_agency_programs(session, agency_id=agency_id)
    data = [
        AgencyProgramResponse(
            id=ap.id,
            program_id=p.id,
            program_code=p.code.value,
            program_name=p.name,
            is_enabled=ap.is_enabled,
            created_at=ap.created_at,
        )
        for ap, p in rows
    ]
    return AgencyProgramListResponse(data=data)


__all__ = ["router"]
