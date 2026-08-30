"""Service-layer logic for the public help/support ticket surface.

Conventions:
  - **No role-dep on PATIENT/GUARDIAN endpoints.** Authentication +
    role check happens via the service layer (`_ensure_*` helpers)
    so the cross-agency / unlinked call returns 404 (not 403) — the
    same convention `src/modules/portal/service.py` uses.
  - **AGENCY_ADMIN endpoints are gated by router-level
    `require_role(AGENCY_ADMIN)`.** Service layer still verifies
    `ticket.agency_id == ctx.agency_id` as defence in depth.
  - **All ticket writes commit + write an audit log.** Audit is
    best-effort (`contextlib.suppress`) per the rest of the
    codebase — a successful user message shouldn't fail because the
    audit log table is locked.

Status transitions:
  - patient/guardian opens / replies  → status stays OPEN until the
    first reply, then flips to AWAITING_REPLY.
  - admin replies → status flips back to OPEN (i.e. waiting on the
    reporter to respond next).
  - admin can flip to RESOLVED / CLOSED via the status PATCH.

The `last_message_at` / `last_message_by_user_id` counters on the
ticket are kept in sync inside the same transaction as the message
insert (so a partial failure never desyncs them).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.exceptions import (
    ForbiddenError,
    InsufficientPermissionsError,
    NotFoundError,
)
from src.modules.audit_logs.service import audit_log, request_ip_ua
from src.modules.identity.dependencies import AuthContext
from src.modules.identity.models import User
from src.modules.patients import service as patients_service
from src.modules.patients.models import (
    GuardianProfile,
    PatientProfile,
)
from src.modules.support.models import SupportTicket, SupportTicketMessage
from src.modules.support.notifications import (
    notify_agency_admins_of_new_ticket,
    notify_reporter_of_admin_reply,
)
from src.shared.domain.enums import (
    AuditAction,
    SupportTicketAuthorKind,
    SupportTicketPriority,
    SupportTicketStatus,
    UserRole,
)
from src.shared.utils.labels import patient_initials


# --------------------------------------------------------------------------
# Auth helpers
# --------------------------------------------------------------------------
async def _resolve_caller_to_patients(
    session: AsyncSession,
    *,
    ctx: AuthContext,
) -> set[uuid.UUID]:
    """Return every patient_id the caller (PATIENT/GUARDIAN) is allowed to act for.

    Mirrors `portal_service._resolve_caller_to_patients` so we don't
    need a cross-module dependency.
    """
    if ctx.agency_id is None:
        return set()
    if ctx.role == UserRole.PATIENT:
        patient = (
            await session.execute(
                select(PatientProfile).where(
                    PatientProfile.user_id == ctx.user_id,
                    PatientProfile.agency_id == ctx.agency_id,
                )
            )
        ).scalar_one_or_none()
        if patient is None:
            return set()
        return {patient.id}
    if ctx.role == UserRole.GUARDIAN:
        guardian = (
            await session.execute(
                select(GuardianProfile).where(
                    GuardianProfile.user_id == ctx.user_id,
                    GuardianProfile.agency_id == ctx.agency_id,
                )
            )
        ).scalar_one_or_none()
        if guardian is None:
            return set()
        # Reuse the canonical helper from patients.service — it
        # already filters on valid_from / valid_until.
        return set(
            await patients_service.list_guardian_patient_ids(
                session,
                guardian_id=guardian.id,
                agency_id=ctx.agency_id,
            )
        )
    raise InsufficientPermissionsError(
        message="Support portal endpoints are only available to PATIENT or GUARDIAN.",
        details={"role": ctx.role.value},
    )


def _author_kind_for(ctx: AuthContext) -> SupportTicketAuthorKind:
    if ctx.role == UserRole.PATIENT:
        return SupportTicketAuthorKind.PATIENT
    if ctx.role == UserRole.GUARDIAN:
        return SupportTicketAuthorKind.GUARDIAN
    if ctx.role == UserRole.AGENCY_ADMIN:
        return SupportTicketAuthorKind.AGENCY_ADMIN
    raise InsufficientPermissionsError(
        message="Unsupported role for support actions.",
        details={"role": ctx.role.value},
    )


async def _load_ticket_or_404(
    session: AsyncSession,
    *,
    ticket_id: uuid.UUID,
    agency_id: uuid.UUID | None,
) -> SupportTicket:
    """Fetch a ticket (with messages), optionally tenant-scoped.

    Returns 404 if missing OR if it belongs to another agency — the
    same cross-agency 404-not-403 pattern the portal uses.
    """
    stmt = (
        select(SupportTicket)
        .where(SupportTicket.id == ticket_id)
        .where(SupportTicket.deleted_at.is_(None))
        .options(
            selectinload(SupportTicket.messages).selectinload(
                SupportTicketMessage.ticket
            )
        )
    )
    if agency_id is not None:
        stmt = stmt.where(SupportTicket.agency_id == agency_id)
    ticket = (await session.execute(stmt)).scalar_one_or_none()
    if ticket is None:
        raise NotFoundError(
            message="Support ticket not found.",
            details={"ticket_id": str(ticket_id), "resource": "support_ticket"},
        )
    return ticket


async def _ensure_ticket_visible_to_caller(
    session: AsyncSession,
    *,
    ticket: SupportTicket,
    ctx: AuthContext,
) -> None:
    """Authorize a caller (PATIENT/GUARDIAN) against a ticket.

    Rules:
      - The caller must be able to see the ticket's patient:
          * PATIENT: their own PatientProfile.id == ticket.patient_id
            (NULL = OK if the reporter is the same patient — they
            filed on their own behalf).
          * GUARDIAN: their PatientGuardianRelationship must be
            currently active (valid_from/valid_until window).
      - OR the caller is the original reporter (handles "reporter
        has no patient link because it's a 'can't log in' ticket"
        case where the patient link is intentionally NULL).
    """
    if ctx.role == UserRole.PATIENT:
        if ticket.reporter_user_id == ctx.user_id:
            return
        patient_ids = await _resolve_caller_to_patients(session, ctx=ctx)
        if ticket.patient_id is not None and ticket.patient_id in patient_ids:
            return
        raise NotFoundError(
            message="Support ticket not found.",
            details={"ticket_id": str(ticket.id), "resource": "support_ticket"},
        )
    if ctx.role == UserRole.GUARDIAN:
        if ticket.reporter_user_id == ctx.user_id:
            return
        patient_ids = await _resolve_caller_to_patients(session, ctx=ctx)
        if ticket.patient_id is not None and ticket.patient_id in patient_ids:
            return
        raise NotFoundError(
            message="Support ticket not found.",
            details={"ticket_id": str(ticket.id), "resource": "support_ticket"},
        )
    raise InsufficientPermissionsError(
        message="Support portal endpoints are only available to PATIENT or GUARDIAN.",
        details={"role": ctx.role.value},
    )


async def _ensure_agency_admin_can_view(
    *,
    ticket: SupportTicket,
    ctx: AuthContext,
) -> None:
    if ctx.role != UserRole.AGENCY_ADMIN:
        raise InsufficientPermissionsError(
            message="Only AGENCY_ADMIN can access the agency support inbox.",
            details={"role": ctx.role.value},
        )
    if ctx.agency_id is None or ticket.agency_id != ctx.agency_id:
        raise NotFoundError(
            message="Support ticket not found.",
            details={"ticket_id": str(ticket.id), "resource": "support_ticket"},
        )


# --------------------------------------------------------------------------
# Authz helpers used by the validation in `open_support_ticket`
# --------------------------------------------------------------------------
async def _resolve_guardian_id_for_caller(
    session: AsyncSession,
    *,
    ctx: AuthContext,
    agency_id: uuid.UUID,
) -> uuid.UUID | None:
    if ctx.role != UserRole.GUARDIAN:
        return None
    guardian = (
        await session.execute(
            select(GuardianProfile).where(
                GuardianProfile.user_id == ctx.user_id,
                GuardianProfile.agency_id == agency_id,
            )
        )
    ).scalar_one_or_none()
    return guardian.id if guardian is not None else None


# --------------------------------------------------------------------------
# Response shape builders
# --------------------------------------------------------------------------
_PREVIEW_LIMIT = 200


def _preview(body: str | None) -> str:
    if not body:
        return ""
    body = body.strip().replace("\n", " ")
    if len(body) <= _PREVIEW_LIMIT:
        return body
    return body[: _PREVIEW_LIMIT - 1].rstrip() + "…"


async def _hydrate_user_names(
    session: AsyncSession,
    *,
    reporter_user_id: uuid.UUID | None,
    patient_id: uuid.UUID | None,
) -> tuple[str | None, str | None, str | None]:
    """Return (patient_name, patient_initials, reporter_name) for the response."""
    patient_name: str | None = None
    patient_initials_value: str | None = None
    reporter_name: str | None = None
    # Patient identity
    if patient_id is not None:
        patient = (
            await session.execute(
                select(PatientProfile).where(PatientProfile.id == patient_id)
            )
        ).scalar_one_or_none()
        if patient is not None:
            user = (
                await session.execute(
                    select(User).where(User.id == patient.user_id)
                )
            ).scalar_one_or_none()
            if user is not None:
                patient_name = user.full_name
                patient_initials_value = patient_initials(user.full_name)
    # Reporter identity
    if reporter_user_id is not None:
        user = (
            await session.execute(
                select(User).where(User.id == reporter_user_id)
            )
        ).scalar_one_or_none()
        if user is not None:
            reporter_name = user.full_name
    return patient_name, patient_initials_value, reporter_name


async def _to_response(
    session: AsyncSession,
    *,
    ticket: SupportTicket,
    include_messages: bool = False,
) -> dict[str, Any]:
    """Project a ticket ORM row to the response dict (summary OR detail).

    Eager-loaded relationships the caller is expected to have on the
    ticket when arriving here:
      - `messages` is required for `message_count` /
        `last_message_preview`; we re-fetch the count if it's
        missing for safety.
    """
    # message count + last message preview ----------------------------------
    last_message = None
    if ticket.messages:
        last_message = ticket.messages[-1]
    last_message_preview = _preview(last_message.body) if last_message else None
    message_count = len(ticket.messages) if ticket.messages is not None else 0

    # hydrate identities -----------------------------------------------------
    patient_name, patient_inits, reporter_name = await _hydrate_user_names(
        session,
        reporter_user_id=ticket.reporter_user_id,
        patient_id=ticket.patient_id,
    )

    payload: dict[str, Any] = {
        "id": ticket.id,
        "agency_id": ticket.agency_id,
        "patient_id": ticket.patient_id,
        "patient_name": patient_name,
        "patient_initials": patient_inits,
        "reporter_user_id": ticket.reporter_user_id,
        "reporter_display_name": reporter_name,
        "reporter_kind": ticket.reporter_kind,
        "subject": ticket.subject,
        "status": ticket.status,
        "priority": ticket.priority,
        "last_message_at": ticket.last_message_at,
        "last_message_preview": last_message_preview,
        "resolved_at": ticket.resolved_at,
        "closed_at": ticket.closed_at,
        "created_at": ticket.created_at,
        "updated_at": ticket.updated_at,
        "message_count": message_count,
    }

    if include_messages:
        # Resolve author display names in one go.
        author_user_ids = {m.author_user_id for m in ticket.messages}
        author_rows = (
            await session.execute(
                select(User).where(User.id.in_(author_user_ids))
            )
        ).scalars().all() if author_user_ids else []
        author_lookup = {u.id: u.full_name for u in author_rows}
        payload["messages"] = [
            {
                "id": m.id,
                "ticket_id": m.ticket_id,
                "author_user_id": m.author_user_id,
                "author_kind": m.author_kind,
                "author_display_name": author_lookup.get(m.author_user_id),
                "body": m.body,
                "created_at": m.created_at,
            }
            for m in ticket.messages
        ]
    return payload


# --------------------------------------------------------------------------
# Open / list / get
# --------------------------------------------------------------------------
async def open_support_ticket(
    session: AsyncSession,
    *,
    ctx: AuthContext,
    subject: str,
    body: str,
    priority: SupportTicketPriority,
    patient_id: uuid.UUID | None,
    background_tasks: Any,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> tuple[SupportTicket, dict[str, Any]]:
    """Create a ticket + first message, write audit log, fan out admin notif.

    PATIENT callers may omit `patient_id` (filing about their own
    account). GUARDIAN callers must supply a `patient_id` and the
    service enforces an active legal relationship.
    """
    if ctx.agency_id is None:
        raise InsufficientPermissionsError(
            message="Caller has no agency scope.",
            details={"role": ctx.role.value},
        )

    # ----- 1. Authorization + patient linkage check ----------------------
    allowed_patient_ids = await _resolve_caller_to_patients(session, ctx=ctx)
    if not allowed_patient_ids and ctx.role != UserRole.PATIENT:
        # GUARDIAN with no active relationships can still file about
        # "I can't log in" — but that requires admin intervention
        # and isn't supported in v1. Surface as 404 to avoid leaking.
        raise NotFoundError(
            message="Support ticket not found.",
            details={"resource": "patient_profile"},
        )

    if patient_id is not None:
        if patient_id not in allowed_patient_ids:
            raise NotFoundError(
                message="Support ticket not found.",
                details={
                    "patient_id": str(patient_id),
                    "resource": "patient_profile",
                },
            )
    elif ctx.role == UserRole.GUARDIAN:
        # Guardians must specify which patient this is about.
        raise NotFoundError(
            message="patient_id is required for guardian reporters.",
            details={"reason": "patient_id_required_for_guardian"},
        )

    reporter_kind = _author_kind_for(ctx)
    now = datetime.now(tz=UTC)

    # ----- 2. Insert ticket + first message ------------------------------
    ticket = SupportTicket(
        agency_id=ctx.agency_id,
        patient_id=patient_id,
        reporter_user_id=ctx.user_id,
        reporter_kind=reporter_kind,
        subject=subject,
        status=SupportTicketStatus.OPEN,
        priority=priority,
        last_message_at=now,
        last_message_by_user_id=ctx.user_id,
    )
    session.add(ticket)
    await session.flush()

    first_message = SupportTicketMessage(
        ticket_id=ticket.id,
        author_user_id=ctx.user_id,
        author_kind=reporter_kind,
        body=body,
    )
    session.add(first_message)
    await session.flush()

    # ----- 3. Audit log (best-effort) ------------------------------------
    try:
        await audit_log(
            session,
            agency_id=ctx.agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.SUPPORT_TICKET_OPENED,
            entity_type="support_ticket",
            entity_id=ticket.id,
            new_data={
                "subject": subject,
                "priority": priority.value,
                "patient_id": str(patient_id) if patient_id else None,
                "reporter_kind": reporter_kind.value,
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )
    except Exception:
        # Audit is best-effort — never block the user write.
        pass

    await session.commit()

    # ----- 4. Reload with messages eager-loaded so _to_response works ---
    ticket = await _load_ticket_or_404(
        session,
        ticket_id=ticket.id,
        agency_id=ctx.agency_id,
    )
    response_dict = await _to_response(
        session, ticket=ticket, include_messages=True
    )

    # ----- 5. Fan out admin notification (best-effort, after commit) ---
    try:
        await notify_agency_admins_of_new_ticket(
            background_tasks,
            session,
            actor_user_id=ctx.user_id,
            actor_agency_id=ctx.agency_id,
            actor_role=ctx.role,
            agency_id=ctx.agency_id,
            ticket_id=ticket.id,
            subject=subject,
            preview=body,
        )
    except Exception:
        pass

    return ticket, response_dict


async def list_my_tickets(
    session: AsyncSession,
    *,
    ctx: AuthContext,
    page: int,
    page_size: int,
) -> tuple[list[dict[str, Any]], int]:
    """List tickets the PATIENT/GUARDIAN caller can see (newest activity first)."""
    if ctx.role not in {UserRole.PATIENT, UserRole.GUARDIAN}:
        raise InsufficientPermissionsError(
            message="Support portal endpoints are only available to PATIENT or GUARDIAN.",
            details={"role": ctx.role.value},
        )

    allowed_patient_ids = await _resolve_caller_to_patients(session, ctx=ctx)

    # The reporter_user_id OR a `patient_id IN (...)` filter.
    base_filter = [
        SupportTicket.deleted_at.is_(None),
        SupportTicket.agency_id == ctx.agency_id,
        or_(
            SupportTicket.reporter_user_id == ctx.user_id,
            SupportTicket.patient_id.in_(allowed_patient_ids)
            if allowed_patient_ids
            else SupportTicket.id.is_(None),  # never matches
        ),
    ]

    count_stmt = (
        select(func.count())
        .select_from(SupportTicket)
        .where(and_(*base_filter))
    )
    total = (await session.execute(count_stmt)).scalar_one()

    list_stmt = (
        select(SupportTicket)
        .where(and_(*base_filter))
        .options(selectinload(SupportTicket.messages))
        .order_by(
            SupportTicket.last_message_at.desc().nulls_last(),
            SupportTicket.created_at.desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await session.execute(list_stmt)).scalars().all()
    items = [await _to_response(session, ticket=t) for t in rows]
    return items, int(total)


async def get_my_ticket(
    session: AsyncSession,
    *,
    ctx: AuthContext,
    ticket_id: uuid.UUID,
    include_messages: bool = True,
) -> dict[str, Any]:
    """Read one ticket from a PATIENT/GUARDIAN caller."""
    ticket = await _load_ticket_or_404(
        session, ticket_id=ticket_id, agency_id=None  # auth-checked below
    )
    await _ensure_ticket_visible_to_caller(
        session, ticket=ticket, ctx=ctx
    )
    # _load_ticket_or_404 returned without tenant scope, so reload
    # scoped to the caller's agency for the response.
    if ctx.agency_id is not None:
        ticket = await _load_ticket_or_404(
            session, ticket_id=ticket_id, agency_id=ctx.agency_id
        )
    return await _to_response(
        session, ticket=ticket, include_messages=include_messages
    )


async def reply_to_my_ticket(
    session: AsyncSession,
    *,
    ctx: AuthContext,
    ticket_id: uuid.UUID,
    body: str,
    background_tasks: Any,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> dict[str, Any]:
    """PATIENT/GUARDIAN replies — flips status to AWAITING_REPLY."""
    if ctx.role not in {UserRole.PATIENT, UserRole.GUARDIAN}:
        raise InsufficientPermissionsError(
            message="Support portal endpoints are only available to PATIENT or GUARDIAN.",
            details={"role": ctx.role.value},
        )
    ticket = await _load_ticket_or_404(
        session, ticket_id=ticket_id, agency_id=ctx.agency_id
    )
    await _ensure_ticket_visible_to_caller(
        session, ticket=ticket, ctx=ctx
    )

    now = datetime.now(tz=UTC)
    author_kind = _author_kind_for(ctx)
    message = SupportTicketMessage(
        ticket_id=ticket.id,
        author_user_id=ctx.user_id,
        author_kind=author_kind,
        body=body,
    )
    session.add(message)
    ticket.last_message_at = now
    ticket.last_message_by_user_id = ctx.user_id
    # Patient/guardian just wrote → admin owes the next reply.
    if ticket.status in {
        SupportTicketStatus.OPEN,
        SupportTicketStatus.AWAITING_REPLY,
    }:
        ticket.status = SupportTicketStatus.AWAITING_REPLY

    try:
        await audit_log(
            session,
            agency_id=ticket.agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.SUPPORT_TICKET_REPLIED,
            entity_type="support_ticket",
            entity_id=ticket.id,
            new_data={"by": author_kind.value, "body_preview": _preview(body)},
            ip_address=ip_address,
            user_agent=user_agent,
        )
    except Exception:
        pass

    await session.commit()

    ticket = await _load_ticket_or_404(
        session, ticket_id=ticket.id, agency_id=ticket.agency_id
    )
    response = await _to_response(
        session, ticket=ticket, include_messages=True
    )

    # No notification fan-out on patient reply — admins are already
    # watching the inbox; agency inbox auto-refreshes via the dashboard.

    return response


# --------------------------------------------------------------------------
# Agency-admin side
# --------------------------------------------------------------------------
async def list_agency_inbox(
    session: AsyncSession,
    *,
    ctx: AuthContext,
    page: int,
    page_size: int,
    status_filter: SupportTicketStatus | None = None,
    priority_filter: SupportTicketPriority | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """AGENCY_ADMIN inbox — full ticket list scoped to one agency."""
    if ctx.role != UserRole.AGENCY_ADMIN or ctx.agency_id is None:
        raise InsufficientPermissionsError(
            message="Only AGENCY_ADMIN can access the agency support inbox.",
            details={"role": ctx.role.value},
        )

    filters = [
        SupportTicket.agency_id == ctx.agency_id,
        SupportTicket.deleted_at.is_(None),
    ]
    if status_filter is not None:
        filters.append(SupportTicket.status == status_filter)
    if priority_filter is not None:
        filters.append(SupportTicket.priority == priority_filter)

    count_stmt = (
        select(func.count())
        .select_from(SupportTicket)
        .where(and_(*filters))
    )
    total = (await session.execute(count_stmt)).scalar_one()

    list_stmt = (
        select(SupportTicket)
        .where(and_(*filters))
        .options(selectinload(SupportTicket.messages))
        .order_by(
            SupportTicket.last_message_at.desc().nulls_last(),
            SupportTicket.created_at.desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await session.execute(list_stmt)).scalars().all()
    items = [await _to_response(session, ticket=t) for t in rows]
    return items, int(total)


async def get_agency_ticket(
    session: AsyncSession,
    *,
    ctx: AuthContext,
    ticket_id: uuid.UUID,
    include_messages: bool = True,
) -> dict[str, Any]:
    """AGENCY_ADMIN reads one ticket."""
    ticket = await _load_ticket_or_404(
        session, ticket_id=ticket_id, agency_id=ctx.agency_id
    )
    await _ensure_agency_admin_can_view(ticket=ticket, ctx=ctx)
    return await _to_response(
        session, ticket=ticket, include_messages=include_messages
    )


async def admin_reply_to_ticket(
    session: AsyncSession,
    *,
    ctx: AuthContext,
    ticket_id: uuid.UUID,
    body: str,
    background_tasks: Any,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> dict[str, Any]:
    """AGENCY_ADMIN replies — flips status back to OPEN."""
    if ctx.role != UserRole.AGENCY_ADMIN or ctx.agency_id is None:
        raise InsufficientPermissionsError(
            message="Only AGENCY_ADMIN can reply on behalf of the agency.",
            details={"role": ctx.role.value},
        )
    ticket = await _load_ticket_or_404(
        session, ticket_id=ticket_id, agency_id=ctx.agency_id
    )
    await _ensure_agency_admin_can_view(ticket=ticket, ctx=ctx)

    now = datetime.now(tz=UTC)
    author_kind = SupportTicketAuthorKind.AGENCY_ADMIN
    message = SupportTicketMessage(
        ticket_id=ticket.id,
        author_user_id=ctx.user_id,
        author_kind=author_kind,
        body=body,
    )
    session.add(message)
    ticket.last_message_at = now
    ticket.last_message_by_user_id = ctx.user_id
    # Admin just wrote → reporter owes the next reply.
    if ticket.status in {
        SupportTicketStatus.AWAITING_REPLY,
        SupportTicketStatus.OPEN,
    }:
        ticket.status = SupportTicketStatus.OPEN

    try:
        await audit_log(
            session,
            agency_id=ticket.agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.SUPPORT_TICKET_REPLIED,
            entity_type="support_ticket",
            entity_id=ticket.id,
            new_data={"by": author_kind.value, "body_preview": _preview(body)},
            ip_address=ip_address,
            user_agent=user_agent,
        )
    except Exception:
        pass

    await session.commit()

    ticket = await _load_ticket_or_404(
        session, ticket_id=ticket.id, agency_id=ticket.agency_id
    )
    response = await _to_response(
        session, ticket=ticket, include_messages=True
    )

    try:
        await notify_reporter_of_admin_reply(
            background_tasks,
            session,
            actor_user_id=ctx.user_id,
            actor_agency_id=ctx.agency_id,
            actor_role=ctx.role,
            agency_id=ticket.agency_id,
            ticket_id=ticket.id,
            recipient_user_id=ticket.reporter_user_id,
            subject=ticket.subject,
            preview=body,
        )
    except Exception:
        pass

    return response


async def change_ticket_status(
    session: AsyncSession,
    *,
    ctx: AuthContext,
    ticket_id: uuid.UUID,
    new_status: SupportTicketStatus,
    new_priority: SupportTicketPriority | None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> dict[str, Any]:
    """AGENCY_ADMIN flips status (RESOLVED/CLOSED) and/or priority."""
    if ctx.role != UserRole.AGENCY_ADMIN or ctx.agency_id is None:
        raise InsufficientPermissionsError(
            message="Only AGENCY_ADMIN can change support-ticket status.",
            details={"role": ctx.role.value},
        )
    ticket = await _load_ticket_or_404(
        session, ticket_id=ticket_id, agency_id=ctx.agency_id
    )
    await _ensure_agency_admin_can_view(ticket=ticket, ctx=ctx)

    now = datetime.now(tz=UTC)
    before = {
        "status": ticket.status.value,
        "priority": ticket.priority.value,
    }
    ticket.status = new_status
    if new_priority is not None:
        ticket.priority = new_priority
    if new_status == SupportTicketStatus.RESOLVED and ticket.resolved_at is None:
        ticket.resolved_at = now
    if new_status == SupportTicketStatus.CLOSED and ticket.closed_at is None:
        ticket.closed_at = now

    try:
        await audit_log(
            session,
            agency_id=ticket.agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.SUPPORT_TICKET_STATUS_CHANGED,
            entity_type="support_ticket",
            entity_id=ticket.id,
            old_data=before,
            new_data={
                "status": ticket.status.value,
                "priority": ticket.priority.value,
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )
    except Exception:
        pass

    await session.commit()

    ticket = await _load_ticket_or_404(
        session, ticket_id=ticket.id, agency_id=ticket.agency_id
    )
    return await _to_response(
        session, ticket=ticket, include_messages=True
    )


__all__ = [
    "admin_reply_to_ticket",
    "change_ticket_status",
    "get_agency_ticket",
    "get_my_ticket",
    "list_agency_inbox",
    "list_my_tickets",
    "open_support_ticket",
    "reply_to_my_ticket",
]
