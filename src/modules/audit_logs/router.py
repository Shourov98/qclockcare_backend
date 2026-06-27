"""Audit logs router — `/audit-logs` read endpoints.

Endpoints:
  GET /audit-logs              — paginated list with filters
  GET /audit-logs/{id}         — single log entry

No INSERT/DELETE endpoints. New rows are written via
`audit_logs_service.audit_log(...)` called from other modules.

Auth: AGENCY_ADMIN or SUPER_ADMIN only. Cross-agency reads are blocked
by RLS + an explicit agency_id filter.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.audit_logs import service as audit_logs_service
from src.modules.audit_logs.schemas import AuditLogResponse
from src.modules.identity.dependencies import (
    CurrentAuth,
    get_session_with_auth,
)
from src.shared.domain.enums import AuditAction
from src.shared.schemas.pagination import (
    PaginatedResponse,
    build_offset_response,
)

router = APIRouter(prefix="/audit-logs", tags=["audit-logs"])


@router.get("", response_model=PaginatedResponse[AuditLogResponse])
async def list_audit_logs_endpoint(
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    actor_user_id: Annotated[uuid.UUID | None, Query()] = None,
    entity_type: Annotated[str | None, Query(max_length=255)] = None,
    entity_id: Annotated[uuid.UUID | None, Query()] = None,
    action: Annotated[AuditAction | None, Query()] = None,
    date_from: Annotated[datetime | None, Query()] = None,
    date_to: Annotated[datetime | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    """List audit logs for the caller's agency (or all for SUPER_ADMIN)."""
    rows, total = await audit_logs_service.list_audit_logs(
        session,
        ctx=ctx,
        actor_user_id=actor_user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size,
    )
    items = [AuditLogResponse.model_validate(r) for r in rows]
    return build_offset_response(
        items,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{log_id}", response_model=AuditLogResponse)
async def get_audit_log_endpoint(
    log_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> AuditLogResponse:
    row = await audit_logs_service.get_audit_log(
        session, log_id=log_id, ctx=ctx
    )
    return AuditLogResponse.model_validate(row)


__all__ = ["router"]
