"""Audit logs service — append helper + read queries.

The append helper (`audit_log(...)`) is called by other modules' routers
in the same transaction as the write it's auditing. It does NOT commit
— the caller controls the surrounding transaction so the audit row is
durably linked to the action it describes.

The read helpers (`list_audit_logs`, `get_audit_log`) are scoped to
the caller's agency (or all agencies for SUPER_ADMIN) via the RLS
policies + an explicit agency_id filter.

Extended helpers:
  - `stream_audit_logs_csv(...)` — server-side CSV export
  - `list_audit_log_filter_options(...)` — distinct filter values for the FE
  - `detect_audit_anomalies(...)` — five rule-based heuristics
"""

from __future__ import annotations

import csv
import hashlib
import io
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import ForbiddenError, NotFoundError
from src.modules.audit_logs.models import AuditLog
from src.modules.audit_logs.schemas import (
    AuditLogActorSummary,
    AuditLogAnomaly,
    AuditLogFilterOptionsResponse,
)
from src.modules.identity.dependencies import AuthContext
from src.modules.identity.models import User, UserRoleAssignment
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
def _ensure_audit_log_reader(ctx: AuthContext) -> None:
    """Authorise a caller to read audit logs.

    AGENCY_ADMIN gets scoped reads; SUPER_ADMIN / PLATFORM_ADMIN (with
    SUPPORT scope) gets cross-tenant reads. Other roles are forbidden.
    """
    # NOTE: PLATFORM_ADMIN-with-SUPPORT-scope is enforced at the router
    # layer via `require_any_scope`. At this layer we check the common
    # allow-list so direct service calls also stay safe.
    allowed = {UserRole.AGENCY_ADMIN, UserRole.SUPER_ADMIN, UserRole.PLATFORM_ADMIN}
    if ctx.role not in allowed:
        raise ForbiddenError(
            "Only AGENCY_ADMIN or SUPER_ADMIN may read audit logs.",
            details={"role": ctx.role.value},
        )


def _apply_audit_filters(
    stmt,
    *,
    ctx: AuthContext,
    actor_user_id: uuid.UUID | None,
    entity_type: str | None,
    entity_id: uuid.UUID | None,
    action: AuditAction | None,
    date_from: datetime | None,
    date_to: datetime | None,
):
    """Apply the shared filter set to any audit-log select statement.

    Per-agency scoping is applied for AGENCY_ADMIN and PLATFORM_ADMIN;
    SUPER_ADMIN sees everything. Used by both `list_audit_logs` and
    `stream_audit_logs_csv`.
    """
    if ctx.role == UserRole.AGENCY_ADMIN:
        if ctx.agency_id is None:
            return stmt  # caller treats as no rows
        stmt = stmt.where(AuditLog.agency_id == ctx.agency_id)
    elif ctx.role == UserRole.PLATFORM_ADMIN:
        # PLATFORM_ADMIN reads audit_logs across agencies but cannot see
        # logs above their context (super-admin-only rows don't carry
        # the right scope, but we still scope by what's available).
        if ctx.agency_id is not None:
            stmt = stmt.where(AuditLog.agency_id == ctx.agency_id)
    # SUPER_ADMIN: no agency scope.

    if actor_user_id is not None:
        stmt = stmt.where(AuditLog.actor_user_id == actor_user_id)
    if entity_type is not None:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if entity_id is not None:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    if date_from is not None:
        stmt = stmt.where(AuditLog.created_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(AuditLog.created_at <= date_to)
    return stmt


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
    _ensure_audit_log_reader(ctx)

    if ctx.role == UserRole.AGENCY_ADMIN and ctx.agency_id is None:
        return [], 0

    base = select(AuditLog)
    count_base = select(func.count()).select_from(AuditLog)

    base = _apply_audit_filters(
        base,
        ctx=ctx,
        actor_user_id=actor_user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        date_from=date_from,
        date_to=date_to,
    )
    count_base = _apply_audit_filters(
        count_base,
        ctx=ctx,
        actor_user_id=actor_user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        date_from=date_from,
        date_to=date_to,
    )

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
    _ensure_audit_log_reader(ctx)
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


# --------------------------------------------------------------------------
# CSV export
# --------------------------------------------------------------------------
_CSV_HEADER = (
    "timestamp",
    "actor_email",
    "actor_role",
    "action",
    "entity_type",
    "entity_id",
    "old_data",
    "new_data",
    "ip_address",
    "user_agent",
)


async def _resolve_actor_role(session: AsyncSession, user_id: uuid.UUID | None) -> str | None:
    """Return a best-effort role string for the actor, preferring the most-recent
    assignment row. None when the user is anonymous (e.g. LOGIN_FAILED)."""
    if user_id is None:
        return None
    row = (
        await session.execute(
            select(UserRoleAssignment.role)
            .where(UserRoleAssignment.user_id == user_id)
            .order_by(UserRoleAssignment.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row.value if row is not None else None


async def _resolve_actor_email(session: AsyncSession, user_id: uuid.UUID | None) -> str | None:
    if user_id is None:
        return None
    row = (
        await session.execute(select(User.email).where(User.id == user_id))
    ).scalar_one_or_none()
    return row


async def stream_audit_logs_csv(
    session: AsyncSession,
    *,
    ctx: AuthContext,
    actor_user_id: uuid.UUID | None,
    entity_type: str | None,
    entity_id: uuid.UUID | None,
    action: AuditAction | None,
    date_from: datetime | None,
    date_to: datetime | None,
    limit: int = 50_000,
) -> AsyncIterator[str]:
    """Yield one CSV row (string) at a time.

    First yielded chunk is the header row. Subsequent chunks are data
    rows. Cap on `limit` defaults to 50k, hard cap 100k — the FE
    download button caps its own request, but the cap protects the
    backend if a curl/script tries to drain the whole table.
    """
    _ensure_audit_log_reader(ctx)

    if ctx.role == UserRole.AGENCY_ADMIN and ctx.agency_id is None:
        # Emit only header
        buf = io.StringIO()
        csv.writer(buf).writerow(_CSV_HEADER)
        yield buf.getvalue()
        return

    limit = max(1, min(100_000, limit))

    # Pre-fetch actor metadata for the result set so we don't issue an
    # N+1 query when serialising each row. For very large exports we
    # fall back to per-row lookup.
    stmt = (
        select(AuditLog, User.email)
        .outerjoin(User, User.id == AuditLog.actor_user_id)
        .order_by(AuditLog.created_at.desc(), AuditLog.id)
        .limit(limit)
    )
    stmt = _apply_audit_filters(
        stmt,
        ctx=ctx,
        actor_user_id=actor_user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        date_from=date_from,
        date_to=date_to,
    )

    # Header row
    buf = io.StringIO()
    csv.writer(buf).writerow(_CSV_HEADER)
    yield buf.getvalue()

    # Cache role lookups: same actor typically has one role.
    role_cache: dict[uuid.UUID, str | None] = {}

    rows = (await session.execute(stmt)).all()
    for row, email in rows:
        actor = row.actor_user_id
        if actor not in role_cache:
            role_cache[actor] = await _resolve_actor_role(session, actor)
        buf = io.StringIO()
        csv.writer(buf).writerow(
            [
                row.created_at.isoformat() if row.created_at else "",
                email or "",
                role_cache[actor] or "",
                row.action.value if row.action else "",
                row.entity_type or "",
                str(row.entity_id) if row.entity_id else "",
                _json_or_empty(row.old_data),
                _json_or_empty(row.new_data),
                str(row.ip_address) if row.ip_address else "",
                row.user_agent or "",
            ]
        )
        yield buf.getvalue()


def _json_or_empty(payload: Any) -> str:
    if payload is None:
        return ""
    import json

    try:
        return json.dumps(payload, default=str, ensure_ascii=False)
    except Exception:
        return ""


# --------------------------------------------------------------------------
# Filter dropdown values
# --------------------------------------------------------------------------
async def list_audit_log_filter_options(
    session: AsyncSession,
    *,
    ctx: AuthContext,
) -> AuditLogFilterOptionsResponse:
    """Distinct users / actions / entity_types + date bounds.

    Scoped to the caller's view (AGENCY_ADMIN → own agency,
    SUPER_ADMIN → all). Used by the FE to populate the filter
    dropdowns.
    """
    _ensure_audit_log_reader(ctx)
    if ctx.role == UserRole.AGENCY_ADMIN and ctx.agency_id is None:
        return AuditLogFilterOptionsResponse()

    # Distinct users (with profile info + event count)
    users_stmt = (
        select(
            User.id,
            User.full_name,
            User.email,
            func.count(AuditLog.id),
        )
        .select_from(AuditLog)
        .join(User, User.id == AuditLog.actor_user_id)
        .group_by(User.id, User.full_name, User.email)
        .order_by(func.count(AuditLog.id).desc())
        .limit(200)
    )
    users_stmt = _apply_audit_filters(users_stmt, ctx=ctx, actor_user_id=None,
                                      entity_type=None, entity_id=None, action=None,
                                      date_from=None, date_to=None)
    user_rows = (await session.execute(users_stmt)).all()

    # Best-effort role per user
    actor_ids = [r[0] for r in user_rows]
    roles: dict[uuid.UUID, str | None] = {}
    if actor_ids:
        role_stmt = (
            select(UserRoleAssignment.user_id, UserRoleAssignment.role)
            .where(UserRoleAssignment.user_id.in_(actor_ids))
            .order_by(UserRoleAssignment.created_at.desc())
        )
        for uid, role in (await session.execute(role_stmt)).all():
            roles.setdefault(uid, role.value if role is not None else None)

    users = [
        AuditLogActorSummary(
            user_id=r[0],
            full_name=r[1],
            email=r[2],
            role=roles.get(r[0]),
            event_count=int(r[3]),
        )
        for r in user_rows
    ]

    # Distinct actions
    actions_stmt = (
        select(distinct(AuditLog.action))
        .order_by(AuditLog.action)
    )
    actions_stmt = _apply_audit_filters(actions_stmt, ctx=ctx, actor_user_id=None,
                                        entity_type=None, entity_id=None, action=None,
                                        date_from=None, date_to=None)
    actions = [a for a, in (await session.execute(actions_stmt)).all() if a is not None]

    # Distinct entity_types
    entity_stmt = (
        select(distinct(AuditLog.entity_type))
        .order_by(AuditLog.entity_type)
    )
    entity_stmt = _apply_audit_filters(entity_stmt, ctx=ctx, actor_user_id=None,
                                       entity_type=None, entity_id=None, action=None,
                                       date_from=None, date_to=None)
    entity_types = [e for e, in (await session.execute(entity_stmt)).all() if e]

    # Date bounds
    min_stmt = select(func.min(AuditLog.created_at))
    min_stmt = _apply_audit_filters(min_stmt, ctx=ctx, actor_user_id=None,
                                    entity_type=None, entity_id=None, action=None,
                                    date_from=None, date_to=None)
    max_stmt = select(func.max(AuditLog.created_at))
    max_stmt = _apply_audit_filters(max_stmt, ctx=ctx, actor_user_id=None,
                                    entity_type=None, entity_id=None, action=None,
                                    date_from=None, date_to=None)
    date_min = (await session.execute(min_stmt)).scalar_one_or_none()
    date_max = (await session.execute(max_stmt)).scalar_one_or_none()

    return AuditLogFilterOptionsResponse(
        users=users,
        actions=actions,
        entity_types=entity_types,
        date_min=date_min,
        date_max=date_max,
    )


# --------------------------------------------------------------------------
# Anomaly detection (rule-based, deterministic)
# --------------------------------------------------------------------------
_SEVERITY_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


def _anomaly_id(rule: str, key: str, window_start: datetime) -> str:
    """Deterministic id so the FE can dedupe across re-fetches."""
    h = hashlib.sha1(f"{rule}|{key}|{window_start.isoformat()}".encode("utf-8"))
    return h.hexdigest()[:32]


async def _actor_display_name(
    session: AsyncSession, user_id: uuid.UUID | None
) -> str | None:
    if user_id is None:
        return None
    row = (
        await session.execute(
            select(User.full_name, User.email).where(User.id == user_id)
        )
    ).first()
    if row is None:
        return None
    full_name, email = row
    return full_name or email


async def _detect_override_bursts(
    session: AsyncSession, ctx: AuthContext, *, window_start: datetime
) -> list[AuditLogAnomaly]:
    """Rule 1: same actor produces ≥3 'override' actions within 2h."""
    actions = (
        AuditAction.APPOINTMENT_MARKED_READY,
        AuditAction.APPOINTMENT_ASSIGNED,
        AuditAction.STATUS_TRANSITION,
        AuditAction.APPOINTMENT_CANCELLED,
    )
    stmt = (
        select(
            AuditLog.actor_user_id,
            func.count(AuditLog.id),
            func.min(AuditLog.created_at),
            func.max(AuditLog.created_at),
            func.array_agg(AuditLog.id),
        )
        .where(AuditLog.created_at >= window_start)
        .where(AuditLog.action.in_(actions))
        .where(AuditLog.actor_user_id.is_not(None))
        .group_by(AuditLog.actor_user_id)
        .having(func.count(AuditLog.id) >= 3)
        .order_by(func.count(AuditLog.id).desc())
    )
    stmt = _apply_audit_filters(stmt, ctx=ctx, actor_user_id=None,
                                entity_type=None, entity_id=None, action=None,
                                date_from=None, date_to=None)
    rows = (await session.execute(stmt)).all()
    out: list[AuditLogAnomaly] = []
    for actor_id, count, wmin, wmax, ids in rows:
        ids_list = list(ids or [])
        if not ids_list:
            continue
        actor_name = await _actor_display_name(session, actor_id)
        # Cap ids array
        capped, truncated = _cap_ids(ids_list)
        out.append(
            AuditLogAnomaly(
                id=_anomaly_id("OVERRIDE_BURST", str(actor_id), wmin or window_start),
                rule="OVERRIDE_BURST",
                severity="MEDIUM",
                title=f"{count} override actions by {actor_name or 'one actor'} in {((wmax - wmin).total_seconds() / 60):.0f} min",
                description=(
                    "Same actor produced multiple appointment-override actions "
                    "(status changes, reassignments, cancels) within a short "
                    "window. Verify each action was intentional and within policy."
                ),
                actor_user_id=actor_id,
                actor_display_name=actor_name,
                window_start=wmin or window_start,
                window_end=wmax or window_start,
                event_count=int(count),
                audit_log_ids=capped,
                metadata={"truncated": truncated} if truncated else {},
            )
        )
    return out


async def _detect_login_brute_force(
    session: AsyncSession, ctx: AuthContext, *, window_start: datetime
) -> list[AuditLogAnomaly]:
    """Rule 2: ≥5 LOGIN_FAILED events within 15 min, by actor or by ip."""
    stmt = (
        select(
            AuditLog.ip_address,
            AuditLog.actor_user_id,
            func.count(AuditLog.id),
            func.min(AuditLog.created_at),
            func.max(AuditLog.created_at),
            func.array_agg(AuditLog.id),
        )
        .where(AuditLog.created_at >= window_start)
        .where(AuditLog.action == AuditAction.LOGIN_FAILED)
        .group_by(AuditLog.ip_address, AuditLog.actor_user_id)
        .having(func.count(AuditLog.id) >= 5)
        .order_by(func.count(AuditLog.id).desc())
    )
    stmt = _apply_audit_filters(stmt, ctx=ctx, actor_user_id=None,
                                entity_type=None, entity_id=None, action=None,
                                date_from=None, date_to=None)
    rows = (await session.execute(stmt)).all()
    out: list[AuditLogAnomaly] = []
    for ip, actor_id, count, wmin, wmax, ids in rows:
        ids_list = list(ids or [])
        if not ids_list:
            continue
        key = str(actor_id) if actor_id else (ip or "unknown")
        actor_name = await _actor_display_name(session, actor_id)
        capped, truncated = _cap_ids(ids_list)
        # HIGH if any LOGIN followed the burst inside the window
        post_stmt = (
            select(func.count(AuditLog.id))
            .where(AuditLog.created_at >= (wmin or window_start))
            .where(AuditLog.created_at <= (wmax or window_start))
            .where(AuditLog.action == AuditAction.LOGIN)
        )
        if actor_id is not None:
            post_stmt = post_stmt.where(AuditLog.actor_user_id == actor_id)
        else:
            post_stmt = post_stmt.where(AuditLog.ip_address == ip)
        post_count = int((await session.execute(post_stmt)).scalar_one() or 0)
        severity = "HIGH" if post_count > 0 else "MEDIUM"
        out.append(
            AuditLogAnomaly(
                id=_anomaly_id("LOGIN_BRUTE_FORCE", key, wmin or window_start),
                rule="LOGIN_BRUTE_FORCE",
                severity=severity,
                title=f"{count} failed login attempts"
                      + (f" from {ip}" if ip else "")
                      + (
                          f" — followed by {post_count} successful login(s)"
                          if post_count > 0
                          else ""
                      ),
                description=(
                    "Multiple failed login attempts in a short window — "
                    "possible credential stuffing or brute-force attack. "
                    "If followed by a successful login, the credential may "
                    "be compromised and should be rotated."
                ),
                actor_user_id=actor_id,
                actor_display_name=actor_name,
                window_start=wmin or window_start,
                window_end=wmax or window_start,
                event_count=int(count),
                audit_log_ids=capped,
                metadata={"truncated": truncated, "ip": ip,
                          "post_burst_login_count": post_count}
                if truncated or post_count > 0 or ip
                else {},
            )
        )
    return out


async def _detect_off_hours_admin(
    session: AsyncSession, ctx: AuthContext, *, window_start: datetime
) -> list[AuditLogAnomaly]:
    """Rule 3: AGENCY_ADMIN actions outside 06:00–22:00 local."""
    # We use UTC hour-of-day since the backend's timezone is UTC; off-hours
    # is approximated as hour < 6 OR hour >= 22. The FE can refine this.
    stmt = (
        select(
            AuditLog.actor_user_id,
            func.count(AuditLog.id),
            func.min(AuditLog.created_at),
            func.max(AuditLog.created_at),
            func.array_agg(AuditLog.id),
        )
        .where(AuditLog.created_at >= window_start)
        .where(AuditLog.action.in_((AuditAction.CREATE, AuditAction.UPDATE, AuditAction.DELETE)))
        .where(AuditLog.actor_user_id.is_not(None))
        .where(
            func.extract("hour", AuditLog.created_at).notin_(
                list(range(6, 22))
            )
        )
        .group_by(AuditLog.actor_user_id)
        .having(func.count(AuditLog.id) >= 1)
        .order_by(func.count(AuditLog.id).desc())
        .limit(50)
    )
    stmt = _apply_audit_filters(stmt, ctx=ctx, actor_user_id=None,
                                entity_type=None, entity_id=None, action=None,
                                date_from=None, date_to=None)
    rows = (await session.execute(stmt)).all()
    out: list[AuditLogAnomaly] = []
    for actor_id, count, wmin, wmax, ids in rows:
        ids_list = list(ids or [])
        if not ids_list:
            continue
        actor_name = await _actor_display_name(session, actor_id)
        capped, truncated = _cap_ids(ids_list)
        severity = "MEDIUM" if int(count) >= 3 else "LOW"
        out.append(
            AuditLogAnomaly(
                id=_anomaly_id("OFF_HOURS_ADMIN", str(actor_id), wmin or window_start),
                rule="OFF_HOURS_ADMIN",
                severity=severity,
                title=f"{count} off-hours admin actions by {actor_name or 'one actor'}",
                description=(
                    "Actor performed CREATE / UPDATE / DELETE actions outside "
                    "06:00–22:00 UTC. Verify the actor's timezone and confirm "
                    "the activity was legitimate."
                ),
                actor_user_id=actor_id,
                actor_display_name=actor_name,
                window_start=wmin or window_start,
                window_end=wmax or window_start,
                event_count=int(count),
                audit_log_ids=capped,
                metadata={"truncated": truncated} if truncated else {},
            )
        )
    return out


async def _detect_role_escalation(
    session: AsyncSession, ctx: AuthContext, *, window_start: datetime
) -> list[AuditLogAnomaly]:
    """Rule 4: ROLE_GRANTED events in the window."""
    stmt = (
        select(
            AuditLog.actor_user_id,
            AuditLog.entity_id,
            func.count(AuditLog.id),
            func.min(AuditLog.created_at),
            func.max(AuditLog.created_at),
            func.array_agg(AuditLog.id),
        )
        .where(AuditLog.created_at >= window_start)
        .where(AuditLog.action == AuditAction.ROLE_GRANTED)
        .group_by(AuditLog.actor_user_id, AuditLog.entity_id)
        .order_by(func.min(AuditLog.created_at).desc())
    )
    stmt = _apply_audit_filters(stmt, ctx=ctx, actor_user_id=None,
                                entity_type=None, entity_id=None, action=None,
                                date_from=None, date_to=None)
    rows = (await session.execute(stmt)).all()
    out: list[AuditLogAnomaly] = []
    for actor_id, target_id, count, wmin, wmax, ids in rows:
        ids_list = list(ids or [])
        if not ids_list:
            continue
        actor_name = await _actor_display_name(session, actor_id)
        target_name = await _actor_display_name(session, target_id)
        capped, truncated = _cap_ids(ids_list)
        out.append(
            AuditLogAnomaly(
                id=_anomaly_id("ROLE_ESCALATION", f"{actor_id}|{target_id}", wmin or window_start),
                rule="ROLE_ESCALATION",
                severity="MEDIUM",
                title=f"Role granted to {target_name or 'a user'}",
                description=(
                    "A privileged role was granted in the audit window. "
                    "Confirm the grant was approved and aligned with policy."
                ),
                actor_user_id=actor_id,
                actor_display_name=actor_name,
                window_start=wmin or window_start,
                window_end=wmax or window_start,
                event_count=int(count),
                audit_log_ids=capped,
                metadata={"target_user_id": str(target_id) if target_id else None,
                          "truncated": truncated} if truncated else
                         {"target_user_id": str(target_id) if target_id else None},
            )
        )
    return out


async def _detect_billing_webhook_fail(
    session: AsyncSession, ctx: AuthContext, *, window_start: datetime
) -> list[AuditLogAnomaly]:
    """Rule 5: any STRIPE_WEBHOOK event with metadata.error truthy."""
    stmt = (
        select(
            AuditLog.id,
            AuditLog.actor_user_id,
            AuditLog.created_at,
            AuditLog.metadata_,
        )
        .where(AuditLog.created_at >= window_start)
        .where(AuditLog.entity_type == "STRIPE_WEBHOOK")
        .where(AuditLog.metadata_["error"].astext.is_not(None))
        .order_by(AuditLog.created_at.desc())
        .limit(20)
    )
    stmt = _apply_audit_filters(stmt, ctx=ctx, actor_user_id=None,
                                entity_type=None, entity_id=None, action=None,
                                date_from=None, date_to=None)
    rows = (await session.execute(stmt)).all()
    out: list[AuditLogAnomaly] = []
    for log_id, actor_id, when, meta in rows:
        meta = meta or {}
        # group consecutive failures into one anomaly per actor-or-system
        key = str(actor_id) if actor_id else "system"
        # For v1 we just emit one anomaly per failure event
        actor_name = await _actor_display_name(session, actor_id) or "Stripe"
        out.append(
            AuditLogAnomaly(
                id=_anomaly_id("BILLING_WEBHOOK_FAIL", f"{key}|{log_id}", when),
                rule="BILLING_WEBHOOK_FAIL",
                severity="HIGH",
                title=f"Stripe webhook failure: {meta.get('event_type', 'unknown event')}",
                description=(
                    f"A Stripe webhook event failed to process: "
                    f"{meta.get('error', 'unspecified error')}. "
                    f"Check the Stripe dashboard and the application logs."
                ),
                actor_user_id=actor_id,
                actor_display_name=actor_name,
                window_start=when,
                window_end=when,
                event_count=1,
                audit_log_ids=[log_id],
                metadata={"event_type": meta.get("event_type"),
                          "event_id": meta.get("event_id"),
                          "error": meta.get("error")},
            )
        )
    return out


def _cap_ids(ids: list[uuid.UUID]) -> tuple[list[uuid.UUID], bool]:
    """Cap the audit_log_ids list at 100 (50 first, 50 last) to bound payload size."""
    if len(ids) <= 100:
        return ids, False
    return ids[:50] + ids[-50:], True


async def detect_audit_anomalies(
    session: AsyncSession,
    *,
    ctx: AuthContext,
    window_hours: int = 24,
) -> tuple[list[AuditLogAnomaly], int]:
    """Run the 5 anomaly rules over the last `window_hours` window.

    Returns (anomalies, window_hours).
    """
    _ensure_audit_log_reader(ctx)
    window_hours = max(1, min(168, window_hours))  # 1h..7d
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=window_hours)

    # AGENCY_ADMIN with no agency gets nothing
    if ctx.role == UserRole.AGENCY_ADMIN and ctx.agency_id is None:
        return [], window_hours

    # Run the rules sequentially. They're each one indexed GROUP BY
    # query against `audit_logs`, so concurrency here wouldn't help
    # meaningfully and would complicate transaction scope.
    rule_results = [
        await _detect_override_bursts(session, ctx, window_start=window_start),
        await _detect_login_brute_force(session, ctx, window_start=window_start),
        await _detect_off_hours_admin(session, ctx, window_start=window_start),
        await _detect_role_escalation(session, ctx, window_start=window_start),
        await _detect_billing_webhook_fail(session, ctx, window_start=window_start),
    ]
    all_anoms: list[AuditLogAnomaly] = []
    for rule_out in rule_results:
        all_anoms.extend(rule_out)

    # Sort: HIGH first, then MEDIUM, then LOW; ties by recency (window_end DESC)
    all_anoms.sort(
        key=lambda a: (
            -_SEVERITY_RANK.get(a.severity, 0),
            -(a.window_end.timestamp() if a.window_end else 0),
        )
    )
    return all_anoms, window_hours


__all__ = [
    "audit_log",
    "get_audit_log",
    "list_audit_logs",
    "list_audit_log_filter_options",
    "stream_audit_logs_csv",
    "detect_audit_anomalies",
]
