"""Audit logs router — `/audit-logs` read endpoints.

Endpoints:
  GET /audit-logs              — paginated list with filters
  GET /audit-logs/filters      — distinct values for FE filter dropdowns
  GET /audit-logs/anomalies    — rule-based anomaly detection
  GET /audit-logs/export.csv   — streamed CSV download
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
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.audit_logs import service as audit_logs_service
from src.modules.audit_logs.schemas import (
    AuditLogAnomalyResponse,
    AuditLogFilterOptionsResponse,
    AuditLogResponse,
)
from src.modules.identity.dependencies import (
    CurrentAuth,
    get_session_with_auth,
    require_role,
)
from src.modules.identity.scope_deps import require_any_scope
from src.shared.domain.enums import AdminScope, AuditAction, UserRole
from src.shared.schemas.pagination import (
    PaginatedResponse,
    build_offset_response,
)

router = APIRouter(prefix="/audit-logs", tags=["audit-logs"])

# Audit logs are readable by:
#   - SUPER_ADMIN (full cross-tenant)
#   - PLATFORM_ADMIN with SUPPORT scope (cross-tenant)
#   - AGENCY_ADMIN (scoped to their agency via RLS)
# Other roles are rejected. We use a list of deps so the OR semantics
# is preserved — either dep suffices.
_AUDIT_LOG_READERS = [
    Depends(require_role(UserRole.AGENCY_ADMIN)),
    Depends(require_any_scope(AdminScope.SUPPORT)),
]


@router.get(
    "",
    response_model=PaginatedResponse[AuditLogResponse],
    dependencies=_AUDIT_LOG_READERS,
)
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


@router.get(
    "/filters",
    response_model=AuditLogFilterOptionsResponse,
    dependencies=_AUDIT_LOG_READERS,
)
async def audit_log_filters_endpoint(
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> AuditLogFilterOptionsResponse:
    """Distinct users / actions / entity_types + date bounds.

    Powers the FE audit-log page filter dropdowns. Only values that
    actually appear in the caller's scope are returned, so the FE
    renders a tight, meaningful list.
    """
    return await audit_logs_service.list_audit_log_filter_options(session, ctx=ctx)


@router.get(
    "/anomalies",
    response_model=AuditLogAnomalyResponse,
    dependencies=_AUDIT_LOG_READERS,
)
async def audit_log_anomalies_endpoint(
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    window_hours: Annotated[int, Query(ge=1, le=168)] = 24,
) -> AuditLogAnomalyResponse:
    """Rule-based anomaly detection for the audit log.

    Powers the FE purple "Review Anomaly" banner. Runs 5 heuristics
    over the last `window_hours` (default 24, max 168 = 7d):
      - OVERRIDE_BURST       (≥3 override actions per actor in 2h)
      - LOGIN_BRUTE_FORCE    (≥5 LOGIN_FAILED in 15 min)
      - OFF_HOURS_ADMIN      (admin actions outside 06:00–22:00 UTC)
      - ROLE_ESCALATION      (any ROLE_GRANTED event)
      - BILLING_WEBHOOK_FAIL (Stripe webhook failure)
    """
    anomalies, hours = await audit_logs_service.detect_audit_anomalies(
        session, ctx=ctx, window_hours=window_hours
    )
    return AuditLogAnomalyResponse(
        anomalies=anomalies,
        generated_at=datetime.utcnow(),
        window_hours=hours,
    )


@router.get(
    "/export.csv",
    dependencies=_AUDIT_LOG_READERS,
    response_class=StreamingResponse,
)
async def audit_log_export_csv_endpoint(
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    actor_user_id: Annotated[uuid.UUID | None, Query()] = None,
    entity_type: Annotated[str | None, Query(max_length=255)] = None,
    entity_id: Annotated[uuid.UUID | None, Query()] = None,
    action: Annotated[AuditAction | None, Query()] = None,
    date_from: Annotated[datetime | None, Query()] = None,
    date_to: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100_000)] = 50_000,
) -> StreamingResponse:
    """Stream a CSV export of the audit log under the same filter set
    as `GET /audit-logs`. Hard cap 100k rows; default 50k.
    """
    generator = audit_logs_service.stream_audit_logs_csv(
        session,
        ctx=ctx,
        actor_user_id=actor_user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
    )
    filename = f"audit-logs-{datetime.utcnow().strftime('%Y-%m-%d')}.csv"
    return StreamingResponse(
        generator,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.get(
    "/{log_id}",
    response_model=AuditLogResponse,
    dependencies=_AUDIT_LOG_READERS,
)
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
