"""Locations router — `/locations` endpoints.

Endpoints:
  GET    /locations           — list agency's locations (paginated)
  POST   /locations           — create (AGENCY_ADMIN only)
  GET    /locations/{id}      — fetch one (any authenticated caller)
  PATCH  /locations/{id}      — update (AGENCY_ADMIN only)
  DELETE /locations/{id}      — soft delete (AGENCY_ADMIN only)

Reads are scoped by RLS to the caller's agency. Writes require
AGENCY_ADMIN at the same agency; SUPER_ADMIN can act on any agency
via `?agency_id=...` (broadcast-style helper).
"""

from __future__ import annotations

import uuid
from builtins import type as _type
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import ForbiddenError, ValidationError
from src.core.logging import get_logger
from src.modules.audit_logs import service as audit_logs_service
from src.modules.identity.dependencies import (
    CurrentAuth,
    get_session_with_auth,
    require_role,
)
from src.modules.locations import service as locations_service
from src.modules.locations.schemas import (
    LocationCreateRequest,
    LocationListResponse,
    LocationResponse,
    LocationUpdateRequest,
)
from src.shared.domain.enums import AuditAction, UserRole
from src.shared.schemas.pagination import build_offset_response

log = get_logger(__name__)
router = APIRouter(prefix="/locations", tags=["locations"])


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _resolve_agency(
    *,
    ctx: CurrentAuth,
    requested_agency_id: uuid.UUID | None,
) -> uuid.UUID:
    """Pick the agency the request is scoped to.

    AGENCY_ADMIN / STAFF / PATIENT / GUARDIAN: must use their own agency.
    SUPER_ADMIN: must specify `?agency_id=...`.
    """
    if ctx.role == UserRole.SUPER_ADMIN:
        if requested_agency_id is None:
            raise ValidationError(
                "SUPER_ADMIN must specify ?agency_id=... for locations."
            )
        return requested_agency_id
    if ctx.agency_id is None:
        raise ForbiddenError("Caller has no agency context.")
    return ctx.agency_id


# --------------------------------------------------------------------------
# List + create
# --------------------------------------------------------------------------
@router.get("", response_model=LocationListResponse)
async def list_locations_endpoint(
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    include_inactive: Annotated[bool, Query()] = False,
    agency_id: Annotated[uuid.UUID | None, Query()] = None,
) -> LocationListResponse:
    """List the caller's agency locations (paginated, active-only by default)."""
    target_agency_id = _resolve_agency(
        ctx=ctx, requested_agency_id=agency_id
    )
    rows, total = await locations_service.list_locations(
        session,
        agency_id=target_agency_id,
        page=page,
        page_size=page_size,
        include_inactive=include_inactive,
    )
    data = [LocationResponse.model_validate(r) for r in rows]
    body = build_offset_response(
        data, total=total, page=page, page_size=page_size
    )
    return LocationListResponse.model_validate(body)


@router.post(
    "",
    response_model=LocationResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def create_location_endpoint(
    payload: LocationCreateRequest,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    agency_id: Annotated[uuid.UUID | None, Query()] = None,
) -> LocationResponse:
    """Create a new location at the caller's agency."""
    target_agency_id = _resolve_agency(
        ctx=ctx, requested_agency_id=agency_id
    )
    location = await locations_service.create_location(
        session,
        agency_id=target_agency_id,
        payload=payload,
    )
    await session.flush()

    # Best-effort audit hook — never breaks the write.
    ip, ua = audit_logs_service.request_ip_ua(request)
    try:
        await audit_logs_service.audit_log(
            session,
            agency_id=target_agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.CREATE,
            entity_type="LOCATION",
            entity_id=location.id,
            new_data={
                "label": location.label,
                "address_line1": location.address_line1,
                "city": location.city,
                "state": location.state,
                "postal_code": location.postal_code,
            },
            ip_address=ip,
            user_agent=ua,
        )
    except Exception as exc:
        log.warning(
            "locations.create_audit_failed",
            error=_type(exc).__name__,
        )

    await session.commit()
    return LocationResponse.model_validate(location)


# --------------------------------------------------------------------------
# Single-row reads + writes
# --------------------------------------------------------------------------
@router.get("/{location_id}", response_model=LocationResponse)
async def get_location_endpoint(
    location_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> LocationResponse:
    """Fetch one location (RLS scoped to caller's agency)."""
    if ctx.agency_id is None:
        raise ForbiddenError("Caller has no agency context.")
    location = await locations_service.get_location(
        session,
        location_id=location_id,
        agency_id=ctx.agency_id,
    )
    return LocationResponse.model_validate(location)


@router.patch(
    "/{location_id}",
    response_model=LocationResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def update_location_endpoint(
    location_id: uuid.UUID,
    payload: LocationUpdateRequest,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> LocationResponse:
    """Partial update of one location."""
    if ctx.agency_id is None:
        raise ForbiddenError("Caller has no agency context.")
    target_agency_id = ctx.agency_id

    location = await locations_service.update_location(
        session,
        location_id=location_id,
        agency_id=target_agency_id,
        payload=payload,
    )
    await session.flush()

    # Audit hook — log only the fields the caller changed.
    ip, ua = audit_logs_service.request_ip_ua(request)
    try:
        await audit_logs_service.audit_log(
            session,
            agency_id=target_agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.UPDATE,
            entity_type="LOCATION",
            entity_id=location.id,
            new_data=payload.model_dump(exclude_unset=True),
            ip_address=ip,
            user_agent=ua,
        )
    except Exception as exc:
        log.warning(
            "locations.update_audit_failed",
            error=_type(exc).__name__,
        )

    await session.commit()
    return LocationResponse.model_validate(location)


@router.delete(
    "/{location_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def delete_location_endpoint(
    location_id: uuid.UUID,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> Response:
    """Soft-delete a location (preserves history)."""
    if ctx.agency_id is None:
        raise ForbiddenError("Caller has no agency context.")
    target_agency_id = ctx.agency_id

    location = await locations_service.soft_delete_location(
        session,
        location_id=location_id,
        agency_id=target_agency_id,
    )
    await session.flush()

    ip, ua = audit_logs_service.request_ip_ua(request)
    try:
        await audit_logs_service.audit_log(
            session,
            agency_id=target_agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.DELETE,
            entity_type="LOCATION",
            entity_id=location.id,
            new_data={"soft_delete": True},
            ip_address=ip,
            user_agent=ua,
        )
    except Exception as exc:
        log.warning(
            "locations.delete_audit_failed",
            error=_type(exc).__name__,
        )

    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
