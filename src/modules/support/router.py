"""Router — `/portal/support/tickets/...` (PATIENT/GUARDIAN)
                  `/agency/support/tickets/...` (AGENCY_ADMIN).

PATIENT/GUARDIAN endpoints have **no router-level role dependency** —
authorization is enforced in the service layer (`_ensure_*` helpers)
so cross-agency / unlinked calls return 404, mirroring the portal
convention. AGENCY_ADMIN endpoints are gated by `require_role`.

Every write endpoint calls `csrf_protect` to match the rest of the
backend's cookie-auth path.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.logging import get_logger
from src.modules.audit_logs.service import request_ip_ua
from src.modules.identity.cookies import csrf_protect
from src.modules.identity.dependencies import (
    CurrentAuth,
    get_session_with_auth,
    require_role,
)
from src.modules.support import service as support_service
from src.modules.support.schemas import (
    SupportTicketCreateRequest,
    SupportTicketDetailResponse,
    SupportTicketListPagination,
    SupportTicketListResponse,
    SupportTicketReplyRequest,
    SupportTicketResponse,
    SupportTicketStatusRequest,
)
from src.shared.domain.enums import (
    SupportTicketPriority,
    SupportTicketStatus,
    UserRole,
)
from src.shared.schemas.docs import standard_responses

log = get_logger(__name__)

router = APIRouter(tags=["support"])


# --------------------------------------------------------------------------
# PATIENT / GUARDIAN — `/portal/support/tickets/...`
# --------------------------------------------------------------------------
@router.post(
    "/portal/support/tickets",
    response_model=SupportTicketDetailResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(csrf_protect)],
    responses=standard_responses(include=[401, 403, 404, 422]),
    summary="Open a support ticket",
    description=(
        "Create a new ticket + the first message in one transaction. "
        "`patient_id` is optional for PATIENT callers (e.g. 'I can't "
        "log in' — about their own account) and required for GUARDIAN "
        "callers. Active guardian linkage is verified by the service "
        "layer; unlinked patients return 404 rather than 403."
    ),
)
async def open_support_ticket_endpoint(
    payload: SupportTicketCreateRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> SupportTicketDetailResponse:
    ip_address, user_agent = request_ip_ua(request)
    _, data = await support_service.open_support_ticket(
        session,
        ctx=ctx,
        subject=payload.subject,
        body=payload.body,
        priority=payload.priority,
        patient_id=payload.patient_id,
        background_tasks=background_tasks,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return SupportTicketDetailResponse.model_validate(data)


@router.get(
    "/portal/support/tickets",
    response_model=SupportTicketListResponse,
    responses=standard_responses(include=[401, 403]),
    summary="List my support tickets",
    description=(
        "Returns the tickets the calling patient or guardian can see "
        "(their own reports + any ticket linked to a patient they "
        "currently have an active legal relationship to). Newest "
        "activity first; `last_message_at DESC NULLS LAST, created_at DESC`."
    ),
)
async def list_my_support_tickets_endpoint(
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SupportTicketListResponse:
    items, total = await support_service.list_my_tickets(
        session, ctx=ctx, page=page, page_size=page_size
    )
    total_pages = (total + page_size - 1) // page_size if total else 0
    return SupportTicketListResponse(
        data=[SupportTicketResponse.model_validate(i) for i in items],
        pagination=SupportTicketListPagination(
            page=page,
            page_size=page_size,
            total=total,
            total_pages=total_pages,
        ),
    )


@router.get(
    "/portal/support/tickets/{ticket_id}",
    response_model=SupportTicketDetailResponse,
    responses=standard_responses(include=[401, 403, 404]),
    summary="Read a ticket (with messages)",
    description="Returns 404 if the caller isn't authorised to see it.",
)
async def get_my_support_ticket_endpoint(
    ticket_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    include_messages: Annotated[bool, Query()] = True,
) -> SupportTicketDetailResponse:
    data = await support_service.get_my_ticket(
        session, ctx=ctx, ticket_id=ticket_id, include_messages=include_messages
    )
    return SupportTicketDetailResponse.model_validate(data)


@router.post(
    "/portal/support/tickets/{ticket_id}/messages",
    response_model=SupportTicketDetailResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(csrf_protect)],
    responses=standard_responses(include=[401, 403, 404, 422]),
    summary="Reply to a ticket",
    description=(
        "Appends a message + flips the ticket status to "
        "AWAITING_REPLY (admin owes next response). The AGENCY_ADMIN "
        "inbox auto-refreshes; no extra notification is fired."
    ),
)
async def reply_to_my_support_ticket_endpoint(
    ticket_id: uuid.UUID,
    payload: SupportTicketReplyRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> SupportTicketDetailResponse:
    ip_address, user_agent = request_ip_ua(request)
    data = await support_service.reply_to_my_ticket(
        session,
        ctx=ctx,
        ticket_id=ticket_id,
        body=payload.body,
        background_tasks=background_tasks,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return SupportTicketDetailResponse.model_validate(data)


# --------------------------------------------------------------------------
# AGENCY_ADMIN — `/agency/support/tickets/...`
# --------------------------------------------------------------------------
_AGENCY_ADMIN = [Depends(require_role(UserRole.AGENCY_ADMIN))]


@router.get(
    "/agency/support/tickets",
    response_model=SupportTicketListResponse,
    dependencies=_AGENCY_ADMIN,
    responses=standard_responses(include=[401, 403]),
    summary="Agency inbox — list tickets",
    description=(
        "Paginated list scoped to `ctx.agency_id`. Optional filters "
        "by status and priority. Newest activity first."
    ),
)
async def list_agency_support_tickets_endpoint(
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status_filter: Annotated[SupportTicketStatus | None, Query(alias="status")] = None,
    priority_filter: Annotated[SupportTicketPriority | None, Query(alias="priority")] = None,
) -> SupportTicketListResponse:
    items, total = await support_service.list_agency_inbox(
        session,
        ctx=ctx,
        page=page,
        page_size=page_size,
        status_filter=status_filter,
        priority_filter=priority_filter,
    )
    total_pages = (total + page_size - 1) // page_size if total else 0
    return SupportTicketListResponse(
        data=[SupportTicketResponse.model_validate(i) for i in items],
        pagination=SupportTicketListPagination(
            page=page,
            page_size=page_size,
            total=total,
            total_pages=total_pages,
        ),
    )


@router.get(
    "/agency/support/tickets/{ticket_id}",
    response_model=SupportTicketDetailResponse,
    dependencies=_AGENCY_ADMIN,
    responses=standard_responses(include=[401, 403, 404]),
    summary="Agency — read one ticket with its messages",
)
async def get_agency_support_ticket_endpoint(
    ticket_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    include_messages: Annotated[bool, Query()] = True,
) -> SupportTicketDetailResponse:
    data = await support_service.get_agency_ticket(
        session, ctx=ctx, ticket_id=ticket_id, include_messages=include_messages
    )
    return SupportTicketDetailResponse.model_validate(data)


@router.post(
    "/agency/support/tickets/{ticket_id}/messages",
    response_model=SupportTicketDetailResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=_AGENCY_ADMIN + [Depends(csrf_protect)],
    responses=standard_responses(include=[401, 403, 404, 422]),
    summary="Agency — reply to a ticket",
    description=(
        "Appends an admin message and flips the ticket status back to "
        "OPEN (reporter owes the next response). Notifies the reporter "
        "via the existing in-app + email notification pipeline."
    ),
)
async def admin_reply_to_support_ticket_endpoint(
    ticket_id: uuid.UUID,
    payload: SupportTicketReplyRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> SupportTicketDetailResponse:
    ip_address, user_agent = request_ip_ua(request)
    data = await support_service.admin_reply_to_ticket(
        session,
        ctx=ctx,
        ticket_id=ticket_id,
        body=payload.body,
        background_tasks=background_tasks,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return SupportTicketDetailResponse.model_validate(data)


@router.patch(
    "/agency/support/tickets/{ticket_id}/status",
    response_model=SupportTicketDetailResponse,
    dependencies=_AGENCY_ADMIN + [Depends(csrf_protect)],
    responses=standard_responses(include=[401, 403, 404, 422]),
    summary="Agency — change status (and optionally priority)",
    description=(
        "Move the ticket to `AWAITING_REPLY` / `RESOLVED` / `CLOSED`. "
        "`priority` is optional — pass it to bump urgency in the same "
        "call. Stamps `resolved_at` / `closed_at` on the matching "
        "transitions for the dashboard to render 'time to close' stats."
    ),
)
async def change_support_ticket_status_endpoint(
    ticket_id: uuid.UUID,
    payload: SupportTicketStatusRequest,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> SupportTicketDetailResponse:
    ip_address, user_agent = request_ip_ua(request)
    data = await support_service.change_ticket_status(
        session,
        ctx=ctx,
        ticket_id=ticket_id,
        new_status=payload.status,
        new_priority=payload.priority,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return SupportTicketDetailResponse.model_validate(data)


__all__ = ["router"]