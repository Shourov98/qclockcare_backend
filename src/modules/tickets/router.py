"""Tickets router — `/admin/tickets` support ticket endpoints.

Endpoints:
  GET    /admin/tickets               — paginated list with filters
  POST   /admin/tickets               — create a new ticket
  GET    /admin/tickets/stats         — counts by status + priority
  GET    /admin/tickets/{id}          — fetch one with comments + author
  PATCH  /admin/tickets/{id}          — update fields
  DELETE /admin/tickets/{id}          — soft delete
  POST   /admin/tickets/{id}/comments — append a comment

Auth: SUPER_ADMIN (full access) OR PLATFORM_ADMIN with SUPPORT scope.
Other roles are rejected with 403. Tickets are not tenant-scoped.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import ConflictError, NotFoundError
from src.modules.audit_logs.service import request_ip_ua
from src.modules.identity.dependencies import (
    CurrentAuth,
    get_session_with_auth,
)
from src.modules.identity.scope_deps import require_scope
from src.modules.tickets import service as tickets_service
from src.modules.tickets.schemas import (
    TicketCommentCreateRequest,
    TicketCommentResponse,
    TicketCreateRequest,
    TicketListItemResponse,
    TicketListPagination,
    TicketListResponse,
    TicketResponse,
    TicketStatsResponse,
    TicketUpdateRequest,
)
from src.shared.domain.enums import AdminScope, TicketPriority, TicketStatus
from src.shared.schemas.docs import standard_responses

router = APIRouter(prefix="/admin/tickets", tags=["admin-tickets"])

# All routes require SUPER_ADMIN OR PLATFORM_ADMIN with SUPPORT scope.
_TICKETS_AUTH = [Depends(require_scope(AdminScope.SUPPORT))]


@router.get(
    "/stats",
    response_model=TicketStatsResponse,
    dependencies=_TICKETS_AUTH,
    responses=standard_responses(include=[401, 403]),
    summary="Aggregate ticket counts",
    description=(
        "Returns total ticket count plus per-status and per-priority "
        "breakdowns for the dashboard summary cards."
    ),
)
async def get_ticket_stats_endpoint(
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> TicketStatsResponse:
    return TicketStatsResponse.model_validate(
        await tickets_service.get_ticket_stats(session)
    )


@router.get(
    "",
    response_model=TicketListResponse,
    dependencies=_TICKETS_AUTH,
    responses=standard_responses(include=[401, 403]),
    summary="List support tickets",
    description=(
        "Paginated list of tickets with optional filters by status, "
        "priority, assignee, reporter, agency, or free-text search "
        "across title + description. Soft-deleted tickets are hidden "
        "unless `include_deleted=true`."
    ),
)
async def list_tickets_endpoint(
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    search: Annotated[str | None, Query(max_length=255)] = None,
    status_filter: Annotated[TicketStatus | None, Query(alias="status")] = None,
    priority_filter: Annotated[TicketPriority | None, Query(alias="priority")] = None,
    assignee_user_id: Annotated[uuid.UUID | None, Query()] = None,
    reporter_user_id: Annotated[uuid.UUID | None, Query()] = None,
    agency_id: Annotated[uuid.UUID | None, Query()] = None,
    include_deleted: Annotated[bool, Query()] = False,
) -> TicketListResponse:
    items, total = await tickets_service.list_tickets(
        session,
        page=page,
        page_size=page_size,
        search=search,
        status_filter=status_filter,
        priority_filter=priority_filter,
        assignee_user_id=assignee_user_id,
        reporter_user_id=reporter_user_id,
        agency_id=agency_id,
        include_deleted=include_deleted,
    )
    total_pages = (total + page_size - 1) // page_size if total else 0
    return TicketListResponse(
        data=[TicketListItemResponse.model_validate(i) for i in items],
        pagination=TicketListPagination(
            page=page,
            page_size=page_size,
            total=total,
            total_pages=total_pages,
        ),
    )


@router.post(
    "",
    response_model=TicketResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=_TICKETS_AUTH,
    responses=standard_responses(include=[401, 403, 404, 422]),
    summary="Create a ticket",
    description=(
        "Creates a new support ticket. `reporter_user_id` defaults to "
        "the caller if omitted. `agency_id` is optional for cross-"
        "tenant issues. `assignee_user_id` must reference an existing "
        "user if provided."
    ),
)
async def create_ticket_endpoint(
    payload: TicketCreateRequest,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> TicketResponse:
    ip_address, user_agent = request_ip_ua(request)
    reporter_user_id = ctx.user_id
    # The schema doesn't expose reporter_user_id — the reporter is
    # always the caller. (We don't expose "report on behalf of"
    # in v1 because the FE never asks for it.)
    ticket = await tickets_service.create_ticket(
        session,
        reporter_user_id=reporter_user_id,
        title=payload.title,
        description=payload.description,
        priority=payload.priority,
        agency_id=payload.agency_id,
        assignee_user_id=payload.assignee_user_id,
        actor_user_id=ctx.user_id,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    # Reload with comments eager-loaded (empty for a brand-new ticket).
    ticket = await tickets_service.get_ticket(session, ticket_id=ticket.id)
    return TicketResponse.model_validate(
        tickets_service._ticket_to_response(ticket)
    )


@router.get(
    "/{ticket_id}",
    response_model=TicketResponse,
    dependencies=_TICKETS_AUTH,
    responses=standard_responses(include=[401, 403, 404]),
    summary="Fetch a ticket with its comments",
)
async def get_ticket_endpoint(
    ticket_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    include_deleted: Annotated[bool, Query()] = False,
) -> TicketResponse:
    ticket = await tickets_service.get_ticket(
        session, ticket_id=ticket_id, include_deleted=include_deleted
    )
    return TicketResponse.model_validate(
        tickets_service._ticket_to_response(ticket)
    )


@router.patch(
    "/{ticket_id}",
    response_model=TicketResponse,
    dependencies=_TICKETS_AUTH,
    responses=standard_responses(include=[401, 403, 404, 422]),
    summary="Update a ticket",
    description=(
        "Partially updates a ticket. Status changes append a "
        "`STATUS_CHANGE` timeline entry; assignment changes append an "
        "`ASSIGNMENT` timeline entry."
    ),
)
async def update_ticket_endpoint(
    ticket_id: uuid.UUID,
    payload: TicketUpdateRequest,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> TicketResponse:
    ip_address, user_agent = request_ip_ua(request)
    changes = payload.model_dump(exclude_unset=True)
    ticket = await tickets_service.update_ticket(
        session,
        ticket_id=ticket_id,
        changes=changes,
        actor_user_id=ctx.user_id,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return TicketResponse.model_validate(
        tickets_service._ticket_to_response(ticket)
    )


@router.delete(
    "/{ticket_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=_TICKETS_AUTH,
    responses=standard_responses(include=[401, 403, 404]),
    summary="Soft-delete a ticket",
    description=(
        "Marks `deleted_at = now()`. The row is preserved for audit "
        "purposes; pass `?include_deleted=true` on list/get to see it."
    ),
)
async def delete_ticket_endpoint(
    ticket_id: uuid.UUID,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> Response:
    ip_address, user_agent = request_ip_ua(request)
    await tickets_service.soft_delete_ticket(
        session,
        ticket_id=ticket_id,
        actor_user_id=ctx.user_id,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{ticket_id}/comments",
    response_model=TicketCommentResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=_TICKETS_AUTH,
    responses=standard_responses(include=[401, 403, 404, 422]),
    summary="Append a comment or timeline entry",
    description=(
        "Adds a comment to the ticket timeline. `kind=COMMENT` for "
        "regular replies, `kind=STATUS_CHANGE` / `kind=ASSIGNMENT` for "
        "system-generated events (auto-created by `PATCH /tickets/{id}` "
        "— manual insertion is supported for completeness)."
    ),
)
async def add_ticket_comment_endpoint(
    ticket_id: uuid.UUID,
    payload: TicketCommentCreateRequest,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> TicketCommentResponse:
    ip_address, user_agent = request_ip_ua(request)
    comment = await tickets_service.add_ticket_comment(
        session,
        ticket_id=ticket_id,
        author_user_id=ctx.user_id,
        body=payload.body,
        kind=payload.kind,
        event_metadata=payload.event_metadata,
        actor_user_id=ctx.user_id,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return TicketCommentResponse.model_validate(comment)


__all__ = ["router"]