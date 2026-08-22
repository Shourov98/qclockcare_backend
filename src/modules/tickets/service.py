"""Tickets service — list / get / create / update / delete / stats / comments.

All callers are SUPER_ADMIN or PLATFORM_ADMIN with SUPPORT scope. Tickets
are not tenant-scoped (`agency_id` is nullable), so the service does not
apply RLS filters — every authenticated admin sees every non-deleted ticket
by default.

The `code` field (`TK-0001`) is generated on create via a daily sequence
kept in a small helper table so it stays monotonic and human-friendly.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.exceptions import ConflictError, NotFoundError
from src.modules.audit_logs.service import audit_log
from src.modules.identity.models import User
from src.modules.tickets.models import Ticket, TicketComment, TicketCommentKind
from src.shared.domain.enums import AuditAction, TicketPriority, TicketStatus


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
async def _next_ticket_code(session: AsyncSession) -> str:
    """Return the next `TK-XXXX` code for today.

    Uses a small counter row keyed by date so the sequence is per-day
    monotonic and resets each calendar day in UTC. We lock the row
    `FOR UPDATE` to avoid duplicate codes under concurrent inserts.
    """
    from sqlalchemy import text

    today = datetime.now(tz=UTC).date()
    # Lock today's row (or insert it if it doesn't exist).
    row = (
        await session.execute(
            text(
                "INSERT INTO ticket_code_sequence (seq_date, last_value) "
                "VALUES (:today, 0) "
                "ON CONFLICT (seq_date) DO NOTHING "
                "RETURNING seq_date, last_value"
            ),
            {"today": today},
        )
    ).first()
    if row is None:
        # Already existed — fetch + lock.
        row = (
            await session.execute(
                text(
                    "SELECT seq_date, last_value FROM ticket_code_sequence "
                    "WHERE seq_date = :today FOR UPDATE"
                ),
                {"today": today},
            )
        ).first()
    assert row is not None  # invariant: row exists after insert or select
    next_value = row.last_value + 1
    await session.execute(
        text(
            "UPDATE ticket_code_sequence SET last_value = :next "
            "WHERE seq_date = :today"
        ),
        {"next": next_value, "today": today},
    )
    return f"TK-{next_value:04d}"


def _ticket_to_list_item(ticket: Ticket, attachment_count: int):
    """Project a Ticket ORM row to a dict matching `TicketListItemResponse`.

    Returning a dict instead of a Pydantic instance lets the router feed
    it straight into `model_validate`. Avoids re-reading the row.
    """
    return {
        "id": ticket.id,
        "code": ticket.code,
        "title": ticket.title,
        "status": ticket.status,
        "priority": ticket.priority,
        "agency_id": ticket.agency_id,
        "reporter_user_id": ticket.reporter_user_id,
        "assignee_user_id": ticket.assignee_user_id,
        "deleted_at": ticket.deleted_at,
        "created_at": ticket.created_at,
        "updated_at": ticket.updated_at,
        "attachment_count": attachment_count,
    }


def _ticket_to_response(ticket: Ticket) -> dict[str, Any]:
    """Project a Ticket (with comments loaded) into a serializable dict."""
    return {
        "id": ticket.id,
        "code": ticket.code,
        "title": ticket.title,
        "description": ticket.description,
        "status": ticket.status,
        "priority": ticket.priority,
        "agency_id": ticket.agency_id,
        "reporter_user_id": ticket.reporter_user_id,
        "assignee_user_id": ticket.assignee_user_id,
        "deleted_at": ticket.deleted_at,
        "extra": ticket.extra,
        "created_at": ticket.created_at,
        "updated_at": ticket.updated_at,
        "comments": [
            {
                "id": c.id,
                "ticket_id": c.ticket_id,
                "author_user_id": c.author_user_id,
                "author": (
                    {
                        "id": c.author.id,
                        "full_name": c.author.full_name,
                        "email": c.author.email,
                    }
                    if c.author is not None
                    else None
                ),
                "kind": c.kind,
                "body": c.body,
                "event_metadata": c.event_metadata,
                "edited_at": c.edited_at,
                "created_at": c.created_at,
                "updated_at": c.updated_at,
            }
            for c in ticket.comments
        ],
    }


async def _assert_assignee_exists(
    session: AsyncSession, *, user_id: uuid.UUID | None
) -> None:
    if user_id is None:
        return
    exists = (
        await session.execute(select(User.id).where(User.id == user_id))
    ).scalar_one_or_none()
    if exists is None:
        raise NotFoundError(
            message="Assignee user not found.",
            details={"user_id": str(user_id), "resource": "user"},
        )


# --------------------------------------------------------------------------
# List / stats
# --------------------------------------------------------------------------
async def list_tickets(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
    search: str | None = None,
    status_filter: TicketStatus | None = None,
    priority_filter: TicketPriority | None = None,
    assignee_user_id: uuid.UUID | None = None,
    reporter_user_id: uuid.UUID | None = None,
    agency_id: uuid.UUID | None = None,
    include_deleted: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """Return a page of tickets and the total count.

    `attachment_count` is computed in a single GROUP BY subquery and
    joined onto the main row to avoid N+1 queries.
    """
    filters = []
    if not include_deleted:
        filters.append(Ticket.deleted_at.is_(None))
    if status_filter is not None:
        filters.append(Ticket.status == status_filter)
    if priority_filter is not None:
        filters.append(Ticket.priority == priority_filter)
    if assignee_user_id is not None:
        filters.append(Ticket.assignee_user_id == assignee_user_id)
    if reporter_user_id is not None:
        filters.append(Ticket.reporter_user_id == reporter_user_id)
    if agency_id is not None:
        filters.append(Ticket.agency_id == agency_id)
    if search:
        pattern = f"%{search.strip()}%"
        filters.append(
            or_(Ticket.title.ilike(pattern), Ticket.description.ilike(pattern))
        )

    # `attachment_count` per ticket.
    attachment_subq = (
        select(
            TicketComment.ticket_id.label("ticket_id"),
            func.count(TicketComment.id).label("attachment_count"),
        )
        .where(TicketComment.kind == TicketCommentKind.ATTACHMENT)
        .group_by(TicketComment.ticket_id)
        .subquery()
    )

    count_stmt = select(func.count()).select_from(Ticket)
    if filters:
        count_stmt = count_stmt.where(and_(*filters))
    total = (await session.execute(count_stmt)).scalar_one()

    list_stmt = (
        select(
            Ticket,
            func.coalesce(attachment_subq.c.attachment_count, 0).label(
                "attachment_count"
            ),
        )
        .outerjoin(
            attachment_subq, attachment_subq.c.ticket_id == Ticket.id
        )
        .order_by(Ticket.created_at.desc(), Ticket.id)
    )
    if filters:
        list_stmt = list_stmt.where(and_(*filters))
    list_stmt = list_stmt.offset((page - 1) * page_size).limit(page_size)

    rows = (await session.execute(list_stmt)).all()
    items = [_ticket_to_list_item(t, int(ac)) for t, ac in rows]
    return items, total


async def get_ticket(
    session: AsyncSession,
    *,
    ticket_id: uuid.UUID,
    include_deleted: bool = False,
) -> Ticket:
    """Fetch one ticket with comments + author eagerly loaded."""
    filters = [Ticket.id == ticket_id]
    if not include_deleted:
        filters.append(Ticket.deleted_at.is_(None))
    stmt = (
        select(Ticket)
        .where(*filters)
        .options(
            selectinload(Ticket.comments).selectinload(TicketComment.author)
        )
    )
    ticket = (await session.execute(stmt)).scalar_one_or_none()
    if ticket is None:
        raise NotFoundError(
            message="Ticket not found.",
            details={"ticket_id": str(ticket_id), "resource": "ticket"},
        )
    return ticket


async def get_ticket_stats(session: AsyncSession) -> dict[str, Any]:
    """Return counts grouped by status + priority for the dashboard cards."""
    total = (
        await session.execute(
            select(func.count())
            .select_from(Ticket)
            .where(Ticket.deleted_at.is_(None))
        )
    ).scalar_one()

    # Status counts — emit a row per status so missing statuses still
    # appear with a zero count.
    status_counts = {
        TicketStatus.OPEN: 0,
        TicketStatus.IN_PROGRESS: 0,
        TicketStatus.PENDING: 0,
        TicketStatus.RESOLVED: 0,
        TicketStatus.CLOSED: 0,
    }
    rows = (
        await session.execute(
            select(Ticket.status, func.count())
            .where(Ticket.deleted_at.is_(None))
            .group_by(Ticket.status)
        )
    ).all()
    for status_value, count in rows:
        status_counts[status_value] = int(count)

    priority_rows = (
        await session.execute(
            select(Ticket.priority, func.count())
            .where(Ticket.deleted_at.is_(None))
            .group_by(Ticket.priority)
        )
    ).all()
    priority_counts = {p: int(c) for p, c in priority_rows}

    return {
        "total": int(total),
        "by_status": status_counts,
        "by_priority": priority_counts,
    }


# --------------------------------------------------------------------------
# Mutations
# --------------------------------------------------------------------------
async def create_ticket(
    session: AsyncSession,
    *,
    reporter_user_id: uuid.UUID,
    title: str,
    description: str,
    priority: TicketPriority = TicketPriority.MEDIUM,
    agency_id: uuid.UUID | None = None,
    assignee_user_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> Ticket:
    """Create a new ticket + audit row.

    `reporter_user_id` is recorded as the requester (defaults to
    `actor_user_id` when omitted). `assignee_user_id` must reference an
    existing user.
    """
    await _assert_assignee_exists(session, user_id=assignee_user_id)

    code = await _next_ticket_code(session)
    ticket = Ticket(
        code=code,
        title=title,
        description=description,
        status=TicketStatus.OPEN,
        priority=priority,
        agency_id=agency_id,
        reporter_user_id=reporter_user_id,
        assignee_user_id=assignee_user_id,
    )
    session.add(ticket)
    await session.flush()

    await audit_log(
        session,
        agency_id=agency_id,
        actor_user_id=actor_user_id,
        action=AuditAction.CREATE,
        entity_type="ticket",
        entity_id=ticket.id,
        new_data={
            "code": ticket.code,
            "title": ticket.title,
            "status": ticket.status.value,
            "priority": ticket.priority.value,
            "agency_id": str(ticket.agency_id) if ticket.agency_id else None,
            "reporter_user_id": str(ticket.reporter_user_id),
            "assignee_user_id": (
                str(ticket.assignee_user_id) if ticket.assignee_user_id else None
            ),
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )

    await session.commit()
    await session.refresh(ticket)
    return ticket


async def update_ticket(
    session: AsyncSession,
    *,
    ticket_id: uuid.UUID,
    changes: dict[str, Any],
    actor_user_id: uuid.UUID | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> Ticket:
    """Patch a ticket.

    Status transitions append a `STATUS_CHANGE` comment when the status
    actually moves (the dashboard renders comments as a timeline).
    Assignment changes append an `ASSIGNMENT` comment.
    """
    ticket = await get_ticket(session, ticket_id=ticket_id, include_deleted=True)

    if "assignee_user_id" in changes:
        await _assert_assignee_exists(
            session, user_id=changes["assignee_user_id"]
        )

    before = {
        "title": ticket.title,
        "status": ticket.status.value if ticket.status else None,
        "priority": ticket.priority.value if ticket.priority else None,
        "agency_id": str(ticket.agency_id) if ticket.agency_id else None,
        "assignee_user_id": (
            str(ticket.assignee_user_id) if ticket.assignee_user_id else None
        ),
    }

    # Apply changes.
    if "title" in changes:
        ticket.title = changes["title"]
    if "description" in changes:
        ticket.description = changes["description"]
    if "priority" in changes:
        ticket.priority = changes["priority"]
    if "agency_id" in changes:
        ticket.agency_id = changes["agency_id"]
    if "assignee_user_id" in changes:
        ticket.assignee_user_id = changes["assignee_user_id"]
    new_status: TicketStatus | None = None
    if "status" in changes:
        new_status = changes["status"]
        if ticket.status != new_status:
            session.add(
                TicketComment(
                    ticket_id=ticket.id,
                    author_user_id=actor_user_id or ticket.reporter_user_id,
                    kind=TicketCommentKind.STATUS_CHANGE,
                    body=f"Status changed: {ticket.status.value} → {new_status.value}",
                    event_metadata={
                        "from": ticket.status.value,
                        "to": new_status.value,
                    },
                )
            )
            ticket.status = new_status

    if (
        "assignee_user_id" in changes
        and ticket.assignee_user_id != before["assignee_user_id"]
    ):
        session.add(
            TicketComment(
                ticket_id=ticket.id,
                author_user_id=actor_user_id or ticket.reporter_user_id,
                kind=TicketCommentKind.ASSIGNMENT,
                body=(
                    "Assigned."
                    if ticket.assignee_user_id is not None
                    else "Unassigned."
                ),
                event_metadata={
                    "assignee_user_id": (
                        str(ticket.assignee_user_id)
                        if ticket.assignee_user_id
                        else None
                    ),
                },
            )
        )

    await session.flush()
    after = {
        "title": ticket.title,
        "status": ticket.status.value if ticket.status else None,
        "priority": ticket.priority.value if ticket.priority else None,
        "agency_id": str(ticket.agency_id) if ticket.agency_id else None,
        "assignee_user_id": (
            str(ticket.assignee_user_id) if ticket.assignee_user_id else None
        ),
    }
    await audit_log(
        session,
        agency_id=ticket.agency_id,
        actor_user_id=actor_user_id,
        action=AuditAction.UPDATE,
        entity_type="ticket",
        entity_id=ticket.id,
        old_data=before,
        new_data=after,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    await session.commit()

    return await get_ticket(session, ticket_id=ticket.id)


async def soft_delete_ticket(
    session: AsyncSession,
    *,
    ticket_id: uuid.UUID,
    actor_user_id: uuid.UUID | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Mark a ticket as deleted (sets `deleted_at = now()`)."""
    ticket = await get_ticket(session, ticket_id=ticket_id, include_deleted=True)
    if ticket.deleted_at is not None:
        # Idempotent — already soft-deleted.
        return
    ticket.deleted_at = datetime.now(tz=UTC)
    await audit_log(
        session,
        agency_id=ticket.agency_id,
        actor_user_id=actor_user_id,
        action=AuditAction.DELETE,
        entity_type="ticket",
        entity_id=ticket.id,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    await session.commit()


# --------------------------------------------------------------------------
# Comments
# --------------------------------------------------------------------------
async def add_ticket_comment(
    session: AsyncSession,
    *,
    ticket_id: uuid.UUID,
    author_user_id: uuid.UUID,
    body: str,
    kind: TicketCommentKind = TicketCommentKind.COMMENT,
    event_metadata: dict[str, Any] | None = None,
    actor_user_id: uuid.UUID | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> TicketComment:
    """Append a comment to a ticket. Returns the new comment."""
    # Verify ticket exists (and is not deleted).
    await get_ticket(session, ticket_id=ticket_id)

    comment = TicketComment(
        ticket_id=ticket_id,
        author_user_id=author_user_id,
        kind=kind,
        body=body,
        event_metadata=event_metadata or {},
    )
    session.add(comment)
    await session.flush()

    # Audit the comment — same as other writes. We log COMMENT events
    # discretely so admins can prove the timeline was appended.
    await audit_log(
        session,
        agency_id=None,
        actor_user_id=actor_user_id,
        action=AuditAction.CREATE,
        entity_type="ticket_comment",
        entity_id=comment.id,
        new_data={
            "ticket_id": str(ticket_id),
            "kind": kind.value,
            "body_preview": body[:80],
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )

    await session.commit()
    await session.refresh(comment)
    # Eager-load the author so the response has `author` populated.
    stmt = (
        select(TicketComment)
        .where(TicketComment.id == comment.id)
        .options(selectinload(TicketComment.author))
    )
    return (await session.execute(stmt)).scalar_one()


__all__ = [
    "add_ticket_comment",
    "create_ticket",
    "get_ticket",
    "get_ticket_stats",
    "list_tickets",
    "soft_delete_ticket",
    "update_ticket",
]
