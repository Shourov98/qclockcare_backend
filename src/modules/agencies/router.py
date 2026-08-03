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

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.logging import get_logger
from src.modules.agencies import service as agencies_service
from src.modules.agencies.schemas import (
    AgencyAdminInviteRequest,
    AgencyCreateRequest,
    AgencyListResponse,
    AgencyProgramListResponse,
    AgencyProgramResponse,
    AgencyResponse,
    AgencySubscriptionPackageListResponse,
    AgencySubscriptionPackageResponse,
    AgencyUpdateRequest,
)
from src.modules.audit_logs import service as audit_logs_service
from src.modules.auth import email_service as auth_email
from src.modules.identity.dependencies import (
    CurrentAuth,
    get_session_with_auth,
    require_role,
)
from src.shared.domain.enums import AuditAction, UserRole, UserStatus
from src.shared.schemas.docs import standard_responses
from src.shared.schemas.pagination import build_offset_response

log = get_logger(__name__)

router = APIRouter(prefix="/agencies", tags=["agencies"])

# All agencies routes require SUPER_ADMIN.
_SUPER_ADMIN_ONLY = [Depends(require_role(UserRole.SUPER_ADMIN))]


# --------------------------------------------------------------------------
# Subscription package catalog
# --------------------------------------------------------------------------
@router.get(
    "/subscription-packages",
    response_model=AgencySubscriptionPackageListResponse,
    responses=standard_responses(include=[]),
)
async def list_subscription_packages_endpoint() -> AgencySubscriptionPackageListResponse:
    """List available agency subscription packages."""
    data = [
        AgencySubscriptionPackageResponse.model_validate(package)
        for package in agencies_service.list_subscription_packages()
    ]
    return AgencySubscriptionPackageListResponse(data=data)


# --------------------------------------------------------------------------
# List + create
# --------------------------------------------------------------------------
@router.get(
    "",
    response_model=AgencyListResponse,
    dependencies=_SUPER_ADMIN_ONLY,
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
    background_tasks: BackgroundTasks,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> AgencyResponse:
    """Create a new agency (and optionally attach programs and an admin).

    When `payload.admin` is set, the agency is created **together** with
    an `AGENCY_ADMIN` user bound to it in a single transaction. If the
    admin branch raises (e.g. duplicate email), the agency row is
    rolled back too — no orphan agencies are possible.
    """
    agency, admin_bind_result = await agencies_service.create_agency(
        session, payload=payload
    )

    # Best-effort audit hook — never breaks the write.
    ip, ua = audit_logs_service.request_ip_ua(request)
    try:
        await audit_logs_service.audit_log(
            session,
            agency_id=agency.id,  # now the agency row exists
            actor_user_id=ctx.user_id,
            action=AuditAction.CREATE,
            entity_type="AGENCY",
            entity_id=agency.id,
            new_data={
                "name": agency.name,
                "timezone": agency.timezone,
                "status": agency.status.value,
                "subscription_plan": agency.subscription_plan.value,
                "subscription_price_cents": agency.subscription_price_cents,
                "subscription_billing_cycle": agency.subscription_billing_cycle,
                "initial_program_codes": payload.initial_program_codes,
                "admin_bound": admin_bind_result is not None,
            },
            ip_address=ip,
            user_agent=ua,
        )
        if admin_bind_result is not None:
            await audit_logs_service.audit_log(
                session,
                agency_id=agency.id,
                actor_user_id=ctx.user_id,
                action=AuditAction.CREATE,
                entity_type="AGENCY_ADMIN",
                entity_id=admin_bind_result.user_id,
                new_data={
                    "email": admin_bind_result.email,
                    "status": admin_bind_result.status.value,
                },
                ip_address=ip,
                user_agent=ua,
            )
    except Exception as exc:
        log.warning(
            "agencies.create_audit_failed",
            error=_type(exc).__name__,
        )

    # Schedule the invitation email AFTER commit (deferred network
    # call, same pattern as staff/router.py:228-235).
    if admin_bind_result is not None and admin_bind_result.invitation_token is not None:
        auth_email.send_invitation_email(
            background_tasks,
            to_email=admin_bind_result.email,
            to_name=admin_bind_result.full_name,
            invitation_token=admin_bind_result.invitation_token,
            expires_in_days=settings.INVITATION_TOKEN_EXPIRY_DAYS,
            recipient_user_id=admin_bind_result.user_id,
        )

    await session.commit()
    return AgencyResponse.model_validate(agency)


@router.post(
    "/{agency_id}/admins",
    status_code=status.HTTP_201_CREATED,
    dependencies=_SUPER_ADMIN_ONLY,
    responses=standard_responses(include=[401, 403, 404, 409, 422]),
    summary="Bind an AGENCY_ADMIN to an existing agency",
    description=(
        "Use this endpoint to attach an AGENCY_ADMIN to an existing "
        "agency (orphan-remediation, adding a second admin, or "
        "promoting an existing user). Same atomic transaction as "
        "`POST /agencies` — if the admin branch raises, the agency "
        "is unaffected."
    ),
)
async def add_agency_admin_endpoint(
    agency_id: uuid.UUID,
    payload: AgencyAdminInviteRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> Response:
    """Attach an AGENCY_ADMIN to an existing agency."""
    bind_result = await agencies_service.add_agency_admin(
        session,
        agency_id=agency_id,
        payload=payload,
    )

    ip, ua = audit_logs_service.request_ip_ua(request)
    try:
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.CREATE,
            entity_type="AGENCY_ADMIN",
            entity_id=bind_result.user_id,
            new_data={
                "email": bind_result.email,
                "status": bind_result.status.value,
                "source": "add_agency_admin_endpoint",
            },
            ip_address=ip,
            user_agent=ua,
        )
    except Exception as exc:
        log.warning(
            "agencies.add_admin_audit_failed",
            error=_type(exc).__name__,
        )

    if bind_result.invitation_token is not None:
        auth_email.send_invitation_email(
            background_tasks,
            to_email=bind_result.email,
            to_name=bind_result.full_name,
            invitation_token=bind_result.invitation_token,
            expires_in_days=settings.INVITATION_TOKEN_EXPIRY_DAYS,
            recipient_user_id=bind_result.user_id,
        )

    await session.commit()
    return Response(
        status_code=status.HTTP_201_CREATED,
        content=(
            f'{{"user_id":"{bind_result.user_id}",'
            f'"email":"{bind_result.email}",'
            f'"status":"{bind_result.status.value}"}}'
        ),
        media_type="application/json",
    )


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
