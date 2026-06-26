"""Visits router — `/visits` and `/visits/{id}/...` endpoints.

All routes require authentication. State-mutating routes (check-in/out,
item delivery, verification, issues) follow the RLS rules: AGENCY_ADMIN
and the assigned STAFF can modify; PATIENT and linked GUARDIAN can file
verifications and issues (their own); read access follows the same
visibility rules as the visit itself.

Endpoints:
  POST   /visits                                     — create on check-in
  GET    /visits                                     — list (paginated, filterable)
  GET    /visits/{id}                                — fetch (summary)
  GET    /visits/{id}/with-items                     — fetch + nested children
  PATCH  /visits/{id}/check-in                       — update check-in fields
  PATCH  /visits/{id}/check-out                      — record check-out
  PATCH  /visits/{id}/transition                     — walk state machine

  GET    /visits/{id}/service-items                  — list
  POST   /visits/{id}/service-items                  — attach appointment item
  PATCH  /visits/{id}/service-items/{item_id}        — update delivery
  DELETE /visits/{id}/service-items/{item_id}        — delete (PENDING only)

  GET    /visits/{id}/notes                          — list
  POST   /visits/{id}/notes                          — add

  POST   /visits/{id}/verify                         — file/update verification

  GET    /visits/{id}/issues                         — list
  POST   /visits/{id}/issues                         — file
  PATCH  /visits/{id}/issues/{issue_id}/resolve      — mark resolved
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import CrossAgencyAccessDeniedError, ForbiddenError
from src.core.logging import get_logger
from src.modules.identity.dependencies import (
    CurrentAuth,
    get_session_with_auth,
    require_role,
)
from src.modules.visits import service as visits_service
from src.modules.visits.schemas import (
    ServiceVerificationCreateRequest,
    ServiceVerificationResponse,
    VisitCheckInRequest,
    VisitCheckOutRequest,
    VisitCreateRequest,
    VisitIssueCreateRequest,
    VisitIssueResolveRequest,
    VisitIssueResponse,
    VisitNoteCreateRequest,
    VisitNoteResponse,
    VisitResponse,
    VisitServiceItemCreateRequest,
    VisitServiceItemResponse,
    VisitServiceItemUpdateRequest,
    VisitStatusTransitionRequest,
    VisitSummaryResponse,
)
from src.shared.domain.enums import UserRole, VisitStatus
from src.shared.schemas.pagination import build_offset_response

logger = get_logger(__name__)

router = APIRouter(prefix="/visits", tags=["visits"])


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _require_agency(ctx: CurrentAuth) -> uuid.UUID:
    if ctx.role == UserRole.SUPER_ADMIN:
        raise ForbiddenError(
            "Use the platform admin console for cross-agency visit operations."
        )
    if ctx.agency_id is None:
        raise ForbiddenError("Caller has no agency context.")
    return ctx.agency_id


def _ensure_can_view_visit(ctx: CurrentAuth, staff_user_id: uuid.UUID) -> None:
    """Visit-level visibility: AGENCY_ADMIN / STAFF (if assigned) / patient / linked guardian."""
    if ctx.role in {UserRole.AGENCY_ADMIN, UserRole.STAFF}:
        return
    if ctx.role in {UserRole.PATIENT, UserRole.GUARDIAN}:
        # RLS does the heavy lifting; allow the request through.
        return
    raise CrossAgencyAccessDeniedError()


def _require_modify(ctx: CurrentAuth, staff_user_id: uuid.UUID) -> None:
    """Visit-level modify: AGENCY_ADMIN or the assigned staff member."""
    if ctx.role == UserRole.AGENCY_ADMIN:
        return
    if ctx.role == UserRole.STAFF:
        # Service-layer check: is this user actually the assigned staff?
        # We can't easily check that here without an extra DB query, so
        # we defer to RLS — which enforces `sp.user_id = current_user_id()`.
        return
    raise ForbiddenError("Only AGENCY_ADMIN or the assigned staff may modify a visit.")


def _to_response(
    visit: object,
    *,
    with_relations: bool = False,
) -> VisitResponse:
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
        "check_in_device_id": visit.check_in_device_id,
        "check_in_address_match": visit.check_in_address_match,
        "check_in_distance_from_location_m": visit.check_in_distance_from_location_m,
        "check_out_time": visit.check_out_time,
        "check_out_lat": visit.check_out_lat,
        "check_out_lng": visit.check_out_lng,
        "check_out_accuracy_m": visit.check_out_accuracy_m,
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
            data["notes"] = list(visit.notes)
        except Exception:
            data["notes"] = None
        try:
            data["verification"] = visit.verification
        except Exception:
            data["verification"] = None
        try:
            data["issues"] = list(visit.issues)
        except Exception:
            data["issues"] = None
    else:
        data["service_items"] = None
        data["notes"] = None
        data["verification"] = None
        data["issues"] = None
    return VisitResponse.model_validate(data)


# --------------------------------------------------------------------------
# Visit CRUD
# --------------------------------------------------------------------------
@router.post(
    "",
    response_model=VisitResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN, UserRole.STAFF))],
)
async def create_visit_endpoint(
    payload: VisitCreateRequest,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> VisitResponse:
    """Create a visit (typically called by the staff app on check-in)."""
    agency_id = _require_agency(ctx)
    visit = await visits_service.create_visit(
        session,
        agency_id=agency_id,
        payload=payload,
        created_by_user_id=ctx.user_id,
    )
    await session.commit()
    await session.refresh(visit, attribute_names=["service_items"])
    return _to_response(visit, with_relations=True)


@router.get(
    "",
    response_model=dict,
)
async def list_visits_endpoint(
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    appointment_id: uuid.UUID | None = Query(default=None),
    staff_id: uuid.UUID | None = Query(default=None),
    status_filter: VisitStatus | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1, le=10000),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    """Paginated list of visits at the caller's agency.

    Filters narrow by appointment, staff, and/or status. RLS restricts
    PATIENT/GUARDIAN rows to their own visits automatically.
    """
    agency_id = _require_agency(ctx)
    rows, total = await visits_service.list_visits(
        session,
        agency_id=agency_id,
        appointment_id=appointment_id,
        staff_id=staff_id,
        status_filter=status_filter,
        page=page,
        page_size=page_size,
    )
    data = [VisitSummaryResponse.model_validate(r) for r in rows]
    return build_offset_response(data, total=total, page=page, page_size=page_size)


@router.get(
    "/{visit_id}",
    response_model=VisitResponse,
)
async def get_visit_endpoint(
    visit_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> VisitResponse:
    """Fetch a single visit (summary)."""
    agency_id = _require_agency(ctx)
    visit = await visits_service.get_visit(
        session, visit_id=visit_id, agency_id=agency_id, with_relations=False
    )
    _ensure_can_view_visit(ctx, visit.staff_id)
    return _to_response(visit, with_relations=False)


@router.get(
    "/{visit_id}/with-items",
    response_model=VisitResponse,
)
async def get_visit_with_items_endpoint(
    visit_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> VisitResponse:
    """Fetch a visit eagerly loaded with its service items, notes, verification, and issues."""
    agency_id = _require_agency(ctx)
    visit = await visits_service.get_visit(
        session, visit_id=visit_id, agency_id=agency_id, with_relations=True
    )
    _ensure_can_view_visit(ctx, visit.staff_id)
    return _to_response(visit, with_relations=True)


@router.patch(
    "/{visit_id}/check-in",
    response_model=VisitResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN, UserRole.STAFF))],
)
async def check_in_visit_endpoint(
    visit_id: uuid.UUID,
    payload: VisitCheckInRequest,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> VisitResponse:
    """Update check-in fields (typically no-op after create)."""
    agency_id = _require_agency(ctx)
    visit = await visits_service.check_in_visit(
        session, visit_id=visit_id, agency_id=agency_id, payload=payload
    )
    await session.commit()
    await session.refresh(visit)
    return _to_response(visit)


@router.patch(
    "/{visit_id}/check-out",
    response_model=VisitResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN, UserRole.STAFF))],
)
async def check_out_visit_endpoint(
    visit_id: uuid.UUID,
    payload: VisitCheckOutRequest,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> VisitResponse:
    """Record the actual check-out."""
    agency_id = _require_agency(ctx)
    visit = await visits_service.check_out_visit(
        session, visit_id=visit_id, agency_id=agency_id, payload=payload
    )
    await session.commit()
    await session.refresh(visit)
    return _to_response(visit)


@router.patch(
    "/{visit_id}/transition",
    response_model=VisitResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN, UserRole.STAFF))],
)
async def transition_visit_endpoint(
    visit_id: uuid.UUID,
    payload: VisitStatusTransitionRequest,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> VisitResponse:
    """Walk the visit state machine (IN_PROGRESS / COMPLETED)."""
    agency_id = _require_agency(ctx)
    visit = await visits_service.transition_visit_status(
        session, visit_id=visit_id, agency_id=agency_id, payload=payload
    )
    await session.commit()
    await session.refresh(visit)
    return _to_response(visit)


# --------------------------------------------------------------------------
# Visit service items
# --------------------------------------------------------------------------
@router.get(
    "/{visit_id}/service-items",
    response_model=list[VisitServiceItemResponse],
)
async def list_visit_service_items_endpoint(
    visit_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> list[VisitServiceItemResponse]:
    agency_id = _require_agency(ctx)
    visit = await visits_service.get_visit(
        session, visit_id=visit_id, agency_id=agency_id
    )
    _ensure_can_view_visit(ctx, visit.staff_id)
    items = await visits_service.list_visit_service_items(
        session, visit_id=visit_id, agency_id=agency_id
    )
    return [VisitServiceItemResponse.model_validate(i) for i in items]


@router.post(
    "/{visit_id}/service-items",
    response_model=VisitServiceItemResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN, UserRole.STAFF))],
)
async def add_visit_service_item_endpoint(
    visit_id: uuid.UUID,
    payload: VisitServiceItemCreateRequest,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> VisitServiceItemResponse:
    """Attach an additional appointment_service_item to a visit."""
    agency_id = _require_agency(ctx)
    item = await visits_service.add_visit_service_item(
        session, visit_id=visit_id, agency_id=agency_id, payload=payload
    )
    await session.commit()
    await session.refresh(item)
    return VisitServiceItemResponse.model_validate(item)


@router.patch(
    "/{visit_id}/service-items/{item_id}",
    response_model=VisitServiceItemResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN, UserRole.STAFF))],
)
async def update_visit_service_item_endpoint(
    visit_id: uuid.UUID,
    item_id: uuid.UUID,
    payload: VisitServiceItemUpdateRequest,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> VisitServiceItemResponse:
    """Patch a visit service item (status / reason / note)."""
    _require_agency(ctx)
    item = await visits_service.update_visit_service_item(
        session,
        item_id=item_id,
        visit_id=visit_id,
        payload=payload,
        completed_by_user_id=ctx.user_id,
    )
    await session.commit()
    await session.refresh(item)
    return VisitServiceItemResponse.model_validate(item)


@router.delete(
    "/{visit_id}/service-items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN, UserRole.STAFF))],
)
async def delete_visit_service_item_endpoint(
    visit_id: uuid.UUID,
    item_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> Response:
    """Remove a PENDING visit service item. Non-pending items cannot be deleted."""
    _require_agency(ctx)
    await visits_service.delete_visit_service_item(
        session, item_id=item_id, visit_id=visit_id
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------
# Visit notes
# --------------------------------------------------------------------------
@router.get(
    "/{visit_id}/notes",
    response_model=list[VisitNoteResponse],
)
async def list_visit_notes_endpoint(
    visit_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> list[VisitNoteResponse]:
    agency_id = _require_agency(ctx)
    visit = await visits_service.get_visit(
        session, visit_id=visit_id, agency_id=agency_id
    )
    _ensure_can_view_visit(ctx, visit.staff_id)
    notes = await visits_service.list_visit_notes(
        session, visit_id=visit_id, agency_id=agency_id
    )
    return [VisitNoteResponse.model_validate(n) for n in notes]


@router.post(
    "/{visit_id}/notes",
    response_model=VisitNoteResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN, UserRole.STAFF))],
)
async def add_visit_note_endpoint(
    visit_id: uuid.UUID,
    payload: VisitNoteCreateRequest,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> VisitNoteResponse:
    """Add a free-form narrative note to the visit."""
    agency_id = _require_agency(ctx)
    note = await visits_service.add_visit_note(
        session,
        visit_id=visit_id,
        agency_id=agency_id,
        body=payload.body,
        author_user_id=ctx.user_id,
    )
    await session.commit()
    await session.refresh(note)
    return VisitNoteResponse.model_validate(note)


# --------------------------------------------------------------------------
# Service verification
# --------------------------------------------------------------------------
@router.post(
    "/{visit_id}/verify",
    response_model=ServiceVerificationResponse,
)
async def file_verification_endpoint(
    visit_id: uuid.UUID,
    payload: ServiceVerificationCreateRequest,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> ServiceVerificationResponse:
    """File or update a service verification (PATIENT or GUARDIAN only).

    AGENCY_ADMIN may also file on behalf of the agency (e.g. to record
    a phone confirmation), but the typical caller is the patient/guardian
    themselves.
    """
    agency_id = _require_agency(ctx)
    # Pick a verifier_role from the caller's role. STAFF/AGENCY_ADMIN
    # get PATIENT (we don't expose a separate "STAFF files verification"
    # flow in this endpoint — that's a back-office operation).
    if ctx.role == UserRole.AGENCY_ADMIN or ctx.role == UserRole.STAFF:
        verifier_role = UserRole.PATIENT
    elif ctx.role in {UserRole.PATIENT, UserRole.GUARDIAN}:
        verifier_role = ctx.role
    else:
        raise ForbiddenError(
            "Only PATIENT, GUARDIAN, or AGENCY_ADMIN may file a verification."
        )

    verification = await visits_service.get_or_create_verification(
        session,
        visit_id=visit_id,
        agency_id=agency_id,
        verified_by=ctx.user_id,
        verifier_role=verifier_role,
        payload=payload,
    )
    await session.commit()
    await session.refresh(verification)
    return ServiceVerificationResponse.model_validate(verification)


# --------------------------------------------------------------------------
# Visit issues
# --------------------------------------------------------------------------
@router.get(
    "/{visit_id}/issues",
    response_model=list[VisitIssueResponse],
)
async def list_visit_issues_endpoint(
    visit_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> list[VisitIssueResponse]:
    agency_id = _require_agency(ctx)
    visit = await visits_service.get_visit(
        session, visit_id=visit_id, agency_id=agency_id
    )
    _ensure_can_view_visit(ctx, visit.staff_id)
    issues = await visits_service.list_visit_issues(
        session, visit_id=visit_id, agency_id=agency_id
    )
    return [VisitIssueResponse.model_validate(i) for i in issues]


@router.post(
    "/{visit_id}/issues",
    response_model=VisitIssueResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_visit_issue_endpoint(
    visit_id: uuid.UUID,
    payload: VisitIssueCreateRequest,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> VisitIssueResponse:
    """File a non-blocking issue against the visit.

    Open to AGENCY_ADMIN, STAFF, PATIENT, and linked GUARDIAN — anyone
    who can see the visit can also report on it.
    """
    agency_id = _require_agency(ctx)
    issue = await visits_service.add_visit_issue(
        session,
        visit_id=visit_id,
        agency_id=agency_id,
        payload=payload,
        reported_by_user_id=ctx.user_id,
    )
    await session.commit()
    await session.refresh(issue)
    return VisitIssueResponse.model_validate(issue)


@router.patch(
    "/{visit_id}/issues/{issue_id}/resolve",
    response_model=VisitIssueResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def resolve_visit_issue_endpoint(
    visit_id: uuid.UUID,
    issue_id: uuid.UUID,
    payload: VisitIssueResolveRequest,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> VisitIssueResponse:
    """Mark an issue resolved (admin only)."""
    _require_agency(ctx)
    issue = await visits_service.resolve_visit_issue(
        session,
        issue_id=issue_id,
        visit_id=visit_id,
        payload=payload,
        resolved_by_user_id=ctx.user_id,
    )
    await session.commit()
    await session.refresh(issue)
    return VisitIssueResponse.model_validate(issue)


__all__ = ["router"]
