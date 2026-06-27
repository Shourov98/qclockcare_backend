"""Patient/Guardian portal router — `/portal/visits/...` endpoints.

All routes require authentication with role PATIENT or GUARDIAN.
Cross-agency / unlinked visits return 404 (not 403) to avoid leaking
visit existence to unrelated patients/guardians.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.logging import get_logger
from src.modules.audit_logs import service as audit_logs_service
from src.modules.identity.dependencies import (
    CurrentAuth,
    get_session_with_auth,
)
from src.modules.notifications import integrations as notif_integrations
from src.modules.portal import service as portal_service
from src.modules.portal.schemas import (
    PortalDisputeRequest,
    PortalReportIssueRequest,
    PortalVerifyRequest,
    PortalVisitListItem,
    PortalVisitResponse,
)
from src.modules.visits.schemas import (
    ServiceVerificationResponse,
    VisitIssueResponse,
)
from src.shared.domain.enums import AuditAction

router = APIRouter(prefix="/portal/visits", tags=["portal"])
log = get_logger(__name__)


def _to_response(visit, *, with_relations: bool = False) -> PortalVisitResponse:
    data: dict = {
        "id": visit.id,
        "appointment_id": visit.appointment_id,
        "agency_id": visit.agency_id,
        "staff_id": visit.staff_id,
        "status": visit.status,
        "check_in_time": visit.check_in_time,
        "check_in_lat": visit.check_in_lat,
        "check_in_lng": visit.check_in_lng,
        "check_in_accuracy_m": visit.check_in_accuracy_m,
        "check_in_address_match": visit.check_in_address_match,
        "check_in_distance_from_location_m": visit.check_in_distance_from_location_m,
        "check_out_time": visit.check_out_time,
        "check_out_lat": visit.check_out_lat,
        "check_out_lng": visit.check_out_lng,
        "duration_seconds": visit.duration_seconds,
        "created_at": visit.created_at,
        "updated_at": visit.updated_at,
    }
    if with_relations:
        try:
            data["service_items"] = list(visit.service_items)
        except Exception:
            data["service_items"] = None
        try:
            data["verification"] = visit.verification
        except Exception:
            data["verification"] = None
        try:
            data["issues"] = list(visit.issues)
        except Exception:
            data["issues"] = None
    return PortalVisitResponse.model_validate(data)


@router.get("", response_model=list[PortalVisitListItem])
async def list_my_visits_endpoint(
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PortalVisitListItem]:
    """Visits the calling patient/guardian is allowed to see (newest first)."""
    visits = await portal_service.list_my_visits(
        session, ctx=ctx, limit=limit, offset=offset
    )
    return [PortalVisitListItem.model_validate(v) for v in visits]


@router.get("/{visit_id}", response_model=PortalVisitResponse)
async def get_my_visit_endpoint(
    visit_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> PortalVisitResponse:
    """Single visit + nested service items / verification / issues."""
    visit = await portal_service.load_visit_with_relations(
        session, visit_id=visit_id, ctx=ctx
    )
    log.info(
        "portal.visit.read",
        visit_id=str(visit.id),
        actor_user_id=str(ctx.user_id),
        role=ctx.role.value,
    )
    return _to_response(visit, with_relations=True)


@router.post(
    "/{visit_id}/verify",
    response_model=ServiceVerificationResponse,
    status_code=status.HTTP_200_OK,
)
async def verify_visit_endpoint(
    visit_id: uuid.UUID,
    payload: PortalVerifyRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> ServiceVerificationResponse:
    """File a positive verification (idempotent)."""
    verification = await portal_service.verify_visit(
        session,
        visit_id=visit_id,
        ctx=ctx,
        comment=payload.comment,
    )
    await session.commit()
    await session.refresh(verification)
    # Notify the assigned staff (best-effort). In-app row + PENDING
    # delivery rows are inserted synchronously; provider network
    # calls (SMTP/Twilio) run on BackgroundTasks so an unreachable
    # SMTP server cannot block the response.
    await notif_integrations.notify_verification_status(
        background_tasks,
        session,
        actor_user_id=ctx.user_id,
        actor_agency_id=verification.agency_id,
        actor_role=ctx.role,
        visit_id=visit_id,
        agency_id=verification.agency_id,
        verified=(verification.status.value == "VERIFIED"),
    )
    # Best-effort audit log (never break the write path).
    try:
        ip, ua = audit_logs_service.request_ip_ua(request)
        await audit_logs_service.audit_log(
            session,
            agency_id=verification.agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.SERVICE_VERIFIED,
            entity_type="SERVICE_VERIFICATION",
            entity_id=verification.id,
            new_data={
                "visit_id": str(visit_id),
                "status": verification.status.value,
                "comment": payload.comment,
            },
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    log.info(
        "portal.verification.filed",
        visit_id=str(visit_id),
        actor_user_id=str(ctx.user_id),
        status=verification.status.value,
    )
    return ServiceVerificationResponse.model_validate(verification)


@router.post(
    "/{visit_id}/dispute",
    response_model=ServiceVerificationResponse,
    status_code=status.HTTP_200_OK,
)
async def dispute_visit_endpoint(
    visit_id: uuid.UUID,
    payload: PortalDisputeRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> ServiceVerificationResponse:
    """File a dispute (idempotent)."""
    verification = await portal_service.dispute_visit(
        session,
        visit_id=visit_id,
        ctx=ctx,
        dispute_reason_code=payload.dispute_reason_code,
        comment=payload.comment,
    )
    await session.commit()
    await session.refresh(verification)
    # Notify the assigned staff (best-effort). In-app row + PENDING
    # delivery rows are inserted synchronously; provider network
    # calls (SMTP/Twilio) run on BackgroundTasks so an unreachable
    # SMTP server cannot block the response.
    await notif_integrations.notify_verification_status(
        background_tasks,
        session,
        actor_user_id=ctx.user_id,
        actor_agency_id=verification.agency_id,
        actor_role=ctx.role,
        visit_id=visit_id,
        agency_id=verification.agency_id,
        verified=False,
    )
    # Best-effort audit log (never break the write path).
    try:
        ip, ua = audit_logs_service.request_ip_ua(request)
        await audit_logs_service.audit_log(
            session,
            agency_id=verification.agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.SERVICE_DISPUTED,
            entity_type="SERVICE_VERIFICATION",
            entity_id=verification.id,
            new_data={
                "visit_id": str(visit_id),
                "status": verification.status.value,
                "dispute_reason_code": payload.dispute_reason_code,
                "comment": payload.comment,
            },
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    log.info(
        "portal.verification.disputed",
        visit_id=str(visit_id),
        actor_user_id=str(ctx.user_id),
        reason=payload.dispute_reason_code,
    )
    return ServiceVerificationResponse.model_validate(verification)


@router.post(
    "/{visit_id}/report-issue",
    response_model=VisitIssueResponse,
    status_code=status.HTTP_201_CREATED,
)
async def report_issue_endpoint(
    visit_id: uuid.UUID,
    payload: PortalReportIssueRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> VisitIssueResponse:
    """File a non-blocking issue against the visit."""
    issue = await portal_service.report_issue(
        session,
        visit_id=visit_id,
        ctx=ctx,
        issue_type=payload.issue_type,
        comment=payload.comment,
    )
    await session.commit()
    await session.refresh(issue)
    # Notify the assigned staff (best-effort). In-app row + PENDING
    # delivery rows are inserted synchronously; provider network
    # calls (SMTP/Twilio) run on BackgroundTasks so an unreachable
    # SMTP server cannot block the response.
    await notif_integrations.notify_visit_issue_filed(
        background_tasks,
        session,
        actor_user_id=ctx.user_id,
        actor_agency_id=issue.agency_id,
        actor_role=ctx.role,
        visit_id=visit_id,
        agency_id=issue.agency_id,
        issue_type=issue.issue_type,
    )
    # Best-effort audit log (never break the write path).
    try:
        ip, ua = audit_logs_service.request_ip_ua(request)
        await audit_logs_service.audit_log(
            session,
            agency_id=issue.agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.CREATE,
            entity_type="VISIT_ISSUE",
            entity_id=issue.id,
            new_data={
                "visit_id": str(visit_id),
                "issue_type": payload.issue_type,
                "comment": payload.comment,
            },
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    log.info(
        "portal.issue.filed",
        visit_id=str(visit_id),
        actor_user_id=str(ctx.user_id),
        issue_type=payload.issue_type,
    )
    return VisitIssueResponse.model_validate(issue)


__all__ = ["router"]
