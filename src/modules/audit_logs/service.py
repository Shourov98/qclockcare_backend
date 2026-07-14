"""Audit logs service — append helper + read queries.

The append helper (`audit_log(...)`) is called by other modules' routers
in the same transaction as the write it's auditing. It does NOT commit
— the caller controls the surrounding transaction so the audit row is
durably linked to the action it describes.

The read helpers (`list_audit_logs`, `get_audit_log`) are scoped to
the caller's agency (or all agencies for SUPER_ADMIN) via the RLS
policies + an explicit agency_id filter.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import ForbiddenError, NotFoundError
from src.modules.audit_logs.models import AuditLog
from src.modules.identity.dependencies import AuthContext
from src.shared.domain.enums import AuditAction, UserRole


# --------------------------------------------------------------------------
# Append helper (called by writers)
# --------------------------------------------------------------------------
async def audit_log(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID | None,
    actor_user_id: uuid.UUID | None,
    action: AuditAction,
    entity_type: str,
    entity_id: uuid.UUID | None = None,
    old_data: dict[str, Any] | None = None,
    new_data: dict[str, Any] | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditLog:
    """Append a single audit log row.

    Best-effort: callers should wrap this in try/except if they want
    logging failures to never break the write path.
    """
    row = AuditLog(
        agency_id=agency_id,
        actor_user_id=actor_user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        old_data=old_data,
        new_data=new_data,
        metadata_=metadata or {},
        ip_address=ip_address,
        user_agent=user_agent,
    )
    session.add(row)
    await session.flush()
    return row


def request_ip_ua(request) -> tuple[str | None, str | None]:
    """Extract client IP + User-Agent from a FastAPI Request.

    Returns (ip, user_agent) — both None if not present.
    """
    if request is None:
        return None, None
    # Prefer X-Forwarded-For if behind a proxy; fall back to client.host.
    ip = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else None)
    )
    ua = request.headers.get("user-agent")
    return ip, ua


# --------------------------------------------------------------------------
# Read helpers
# --------------------------------------------------------------------------
async def list_audit_logs(
    session: AsyncSession,
    *,
    ctx: AuthContext,
    actor_user_id: uuid.UUID | None,
    entity_type: str | None,
    entity_id: uuid.UUID | None,
    action: AuditAction | None,
    date_from: datetime | None,
    date_to: datetime | None,
    page: int,
    page_size: int,
) -> tuple[list[AuditLog], int]:
    """List audit logs scoped to the caller's agency (or all for SUPER_ADMIN).

    Returns (rows, total).
    """
    if ctx.role not in {UserRole.AGENCY_ADMIN, UserRole.SUPER_ADMIN}:
        raise ForbiddenError(
            "Only AGENCY_ADMIN or SUPER_ADMIN may read audit logs.",
            details={"role": ctx.role.value},
        )

    base = select(AuditLog)
    count_base = select(func.count()).select_from(AuditLog)

    # Per-agency scoping for AGENCY_ADMIN; SUPER_ADMIN sees all.
    if ctx.role == UserRole.AGENCY_ADMIN:
        if ctx.agency_id is None:
            return [], 0
        base = base.where(AuditLog.agency_id == ctx.agency_id)
        count_base = count_base.where(AuditLog.agency_id == ctx.agency_id)

    if actor_user_id is not None:
        base = base.where(AuditLog.actor_user_id == actor_user_id)
        count_base = count_base.where(AuditLog.actor_user_id == actor_user_id)
    if entity_type is not None:
        base = base.where(AuditLog.entity_type == entity_type)
        count_base = count_base.where(AuditLog.entity_type == entity_type)
    if entity_id is not None:
        base = base.where(AuditLog.entity_id == entity_id)
        count_base = count_base.where(AuditLog.entity_id == entity_id)
    if action is not None:
        base = base.where(AuditLog.action == action)
        count_base = count_base.where(AuditLog.action == action)
    if date_from is not None:
        base = base.where(AuditLog.created_at >= date_from)
        count_base = count_base.where(AuditLog.created_at >= date_from)
    if date_to is not None:
        base = base.where(AuditLog.created_at <= date_to)
        count_base = count_base.where(AuditLog.created_at <= date_to)

    page = max(1, page)
    page_size = max(1, min(100, page_size))
    base = (
        base.order_by(AuditLog.created_at.desc(), AuditLog.id)
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    rows = list((await session.execute(base)).scalars().all())
    total = int((await session.execute(count_base)).scalar_one())
    return rows, total


async def get_audit_log(
    session: AsyncSession,
    *,
    log_id: uuid.UUID,
    ctx: AuthContext,
) -> AuditLog:
    if ctx.role not in {UserRole.AGENCY_ADMIN, UserRole.SUPER_ADMIN}:
        raise ForbiddenError(
            "Only AGENCY_ADMIN or SUPER_ADMIN may read audit logs.",
            details={"role": ctx.role.value},
        )
    row = (
        await session.execute(select(AuditLog).where(AuditLog.id == log_id))
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("Audit log not found.")
    # AGENCY_ADMIN can only see their agency's logs.
    if ctx.role == UserRole.AGENCY_ADMIN and row.agency_id != ctx.agency_id:
        # Return 404 to avoid leaking other agencies' log existence.
        raise NotFoundError("Audit log not found.")
    return row


__all__ = [
    "audit_log",
    "get_audit_log",
    "list_audit_logs",
]
