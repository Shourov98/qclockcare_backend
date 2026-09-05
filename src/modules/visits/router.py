"""Visits router — `/visits` and `/visits/{id}/...` endpoints.

All routes require authentication. State-mutating routes follow the
spec's 5-state lifecycle. The caregiver app POSTs `/visits` (start),
`/end` (EVV end), `/confirm-billing`, `/sign` (multipart), and
`/transition` (state-machine walker). The legacy `/verify`, `/dispute`,
`/issues` endpoints are gone — verification is replaced by the
`AppointmentSignature` row; visit issues are out of scope.

Endpoints:
  POST   /visits                                     — create (READY → IN_PROGRESS)
  GET    /visits                                     — list (paginated, filterable)
  GET    /visits/{id}                                — fetch (summary)
  GET    /visits/{id}/with-items                     — fetch + nested children
  PATCH  /visits/{id}/end                            — record EVV end (caregiver departure)
  PATCH  /visits/{id}/transition                     — walk state machine (spec §5/§6/§8)

  POST   /visits/{id}/confirm-billing                — caregiver ticks billing checkbox

  POST   /visits/{id}/sign                           — multipart: file a signature (spec §8)

  GET    /visits/{id}/activities
  PATCH  /visits/{id}/activities/{activity_id}      — record delivery outcome

  GET    /visits/{id}/notes
  POST   /visits/{id}/notes

  POST   /visits/{id}/start-location-sharing         — opt-in: live GPS
  POST   /visits/{id}/location-ping                  — staff device ping
  POST   /visits/{id}/stop-location-sharing          — opt-out: stop live GPS
"""

from __future__ import annotations

import os
import uuid
from datetime import date, timedelta
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    Query,
    Request,
    status,
    UploadFile,
)
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.exceptions import (
    CrossAgencyAccessDeniedError,
    ForbiddenError,
    ValidationError,
)
from src.core.logging import get_logger
from src.modules.audit_logs import service as audit_logs_service
from src.modules.identity.dependencies import (
    CurrentAuth,
    get_session_with_auth,
    require_role,
)
from src.modules.notifications import integrations as notif_integrations
from src.modules.visits import service as visits_service
from src.modules.visits.schemas import (
    AppointmentSignatureResponse,
    VisitActivityDeliveryResponse,
    VisitActivityUpdateRequest,
    VisitComplianceResponse,
    VisitConfirmBillingRequest,
    VisitCreateRequest,
    VisitEndRequest,
    VisitLocationPingRequest,
    VisitNoteCreateRequest,
    VisitNoteResponse,
    VisitResponse,
    VisitStartLocationSharingRequest,
    VisitStatusTransitionRequest,
    VisitSummaryResponse,
)
from src.shared.domain.enums import AuditAction, UserRole, VisitStatus
from src.shared.schemas.docs import standard_responses
from src.shared.schemas.pagination import build_offset_response
from src.shared.utils.datetime_utils import utc_now

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
    """Visit-level visibility: AGENCY_ADMIN / STAFF / patient / linked guardian."""
    if ctx.role in {UserRole.AGENCY_ADMIN, UserRole.STAFF}:
        return
    if ctx.role in {UserRole.PATIENT, UserRole.GUARDIAN}:
        # RLS does the heavy lifting; allow the request through.
        return
    raise CrossAgencyAccessDeniedError()


def _require_modify_visit(ctx: CurrentAuth) -> None:
    """Visit-level modify: AGENCY_ADMIN or assigned staff only."""
    if ctx.role == UserRole.AGENCY_ADMIN:
        return
    if ctx.role == UserRole.STAFF:
        # Service-layer RLS narrows to the assigned staff_user_id.
        return
    raise ForbiddenError("Only AGENCY_ADMIN or the assigned staff may modify a visit.")


def _save_signature_locally(
    visit_id: uuid.UUID,
    *,
    content: bytes,
    mime_type: str,
) -> str:
    """Persist the signature image to local disk and return a relative
    URL path (e.g. `/static/signatures/{visit_id}.png`) that the FE can
    resolve against the API base.

    Storage layout: `{SIGNATURE_STORAGE_PATH}/{visit_id}.{ext}` where
    `ext` is derived from `mime_type` (png / jpeg). Idempotent — a
    re-sign overwrites the previous file.
    """
    storage_dir = settings.SIGNATURE_STORAGE_PATH
    os.makedirs(storage_dir, exist_ok=True)
    ext = "png" if mime_type.endswith("png") else "jpg"
    fname = f"{visit_id}.{ext}"
    path = os.path.join(storage_dir, fname)
    with open(path, "wb") as fh:
        fh.write(content)
    return f"{settings.SIGNATURE_PUBLIC_URL_PREFIX}/{fname}"


def _to_response(
    visit: object,
    *,
    with_relations: bool = False,
) -> VisitResponse:
    """Build a `VisitResponse`, hydrating every joined display field the
    staff Visit Summary screen needs in one round trip.
    """
    staff_name: str | None = None
    staff_role_label: str | None = None
    try:
        staff = getattr(visit, "staff", None)
        if staff is not None:
            user = getattr(staff, "user", None)
            if user is not None:
                staff_name = getattr(user, "full_name", None)
            # StaffProfile doesn't currently have a `role_label` column;
            # render the raw role enum if any. Falls back to None on
            # unmapped roles so the FE shows "Staff" by default.
            role = getattr(staff, "role", None)
            if role is not None and hasattr(role, "value"):
                staff_role_label = str(role.value)
    except Exception:
        staff_name = None

    data: dict = {
        # raw columns
        "id": visit.id,
        "appointment_id": visit.appointment_id,
        "agency_id": visit.agency_id,
        "staff_id": visit.staff_id,
        "status": visit.status,
        "billing_confirmed_at": getattr(visit, "billing_confirmed_at", None),
        "billing_confirmed_by_user_id": getattr(
            visit, "billing_confirmed_by_user_id", None
        ),
        "live_lat": getattr(visit, "live_lat", None),
        "live_lng": getattr(visit, "live_lng", None),
        "live_ping_at": getattr(visit, "live_ping_at", None),
        "live_accuracy_m": getattr(visit, "live_accuracy_m", None),
        "sharing_location": getattr(visit, "sharing_location", False),
        "created_at": visit.created_at,
        "updated_at": visit.updated_at,
        # joined display
        "staff_name": staff_name,
        "staff_role_label": staff_role_label,
    }
    if with_relations:
        try:
            data["activities"] = list(visit.activity_deliveries)
        except Exception:
            data["activities"] = None
        try:
            data["notes"] = list(visit.notes)
        except Exception:
            data["notes"] = None
        try:
            data["signature"] = visit.signature
        except Exception:
            data["signature"] = None
        try:
            data["evv_record"] = visit.evv_record
        except Exception:
            data["evv_record"] = None
    else:
        data["activities"] = None
        data["notes"] = None
        data["signature"] = None
        data["evv_record"] = None
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
    request: Request,
    background_tasks: BackgroundTasks,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> VisitResponse:
    """Create a visit (READY → IN_PROGRESS on the underlying appointment).

    Called by the staff mobile app on caregiver arrival. The visit row
    starts in `IN_PROGRESS`; an `EVVRecord.start_*` is stamped from the
    supplied GPS; one `VisitActivityDelivery` per parent activity is
    seeded.
    """
    agency_id = _require_agency(ctx)
    visit = await visits_service.create_visit(
        session,
        agency_id=agency_id,
        payload=payload,
        created_by_user_id=ctx.user_id,
    )
    await session.commit()
    await session.refresh(visit)
    # Best-effort audit log.
    try:
        ip, ua = audit_logs_service.request_ip_ua(request)
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.VISIT_STARTED,
            entity_type="VISIT",
            entity_id=visit.id,
            new_data={
                "appointment_id": str(visit.appointment_id),
                "staff_id": str(visit.staff_id),
                "status": visit.status.value,
            },
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    # Fan-out VISIT_STARTED to patient + guardians. In-app row written
    # synchronously; provider network calls deferred to BackgroundTasks.
    await notif_integrations.notify_visit_started(
        background_tasks,
        session,
        actor_user_id=ctx.user_id,
        actor_agency_id=agency_id,
        actor_role=ctx.role,
        visit_id=visit.id,
        agency_id=agency_id,
    )
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
    patient_id: uuid.UUID | None = Query(default=None),
    status_filter: VisitStatus | None = Query(default=None, alias="status"),
    sharing_only: bool = Query(default=False),
    page: int = Query(default=1, ge=1, le=10000),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    """Paginated list of visits at the caller's agency.

    Filters narrow by appointment, staff, patient, and/or status. The
    `sharing_only` flag is used by the EVV Live Monitor to drop visits
    that aren't currently streaming GPS without burning extra queries.
    RLS restricts PATIENT/GUARDIAN rows to their own visits automatically.
    """
    agency_id = _require_agency(ctx)
    rows, total = await visits_service.list_visits(
        session,
        agency_id=agency_id,
        appointment_id=appointment_id,
        staff_id=staff_id,
        patient_id=patient_id,
        status_filter=status_filter,
        sharing_only=sharing_only,
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
    """Fetch a visit eagerly loaded with activities, notes, signature, and EVV."""
    agency_id = _require_agency(ctx)
    visit = await visits_service.get_visit(
        session, visit_id=visit_id, agency_id=agency_id, with_relations=True
    )
    _ensure_can_view_visit(ctx, visit.staff_id)
    return _to_response(visit, with_relations=True)


@router.get(
    "/{visit_id}/compliance",
    response_model=VisitComplianceResponse,
)
async def get_visit_compliance_endpoint(
    visit_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> VisitComplianceResponse:
    """Computed-on-read 5-row compliance rollup for the visit.

    Powers the `ComplianceCard` widget on the patient/guardian visit
    detail page. All four checks (EVV / notes / signature / billing)
    run in a single eager-loaded query.

    Visible to AGENCY_ADMIN, STAFF, and PATIENT/GUARDIAN — RLS narrows
    the latter two to visits they own. Unauthorised reads return 404
    so existence isn't leaked.
    """
    agency_id = _require_agency(ctx)
    # Visibility check first — patients/guardians only see visits they're
    # linked to; staff + admin pass via the helper.
    existing = await visits_service.get_visit(
        session, visit_id=visit_id, agency_id=agency_id, with_relations=False
    )
    _ensure_can_view_visit(ctx, existing.staff_id)
    return await visits_service.get_visit_compliance(
        session, visit_id=visit_id, agency_id=agency_id
    )


@router.patch(
    "/{visit_id}/end",
    response_model=VisitResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN, UserRole.STAFF))],
)
async def end_visit_endpoint(
    visit_id: uuid.UUID,
    payload: VisitEndRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> VisitResponse:
    """Record the EVV End block (caregiver departure).

    Per spec §10, the visit's `EVVRecord.end_*` fields are stamped.
    This does NOT auto-progress the visit status — the caregiver must
    call `/confirm-billing` then `/transition` to AWAITING_SIGNATURE.
    """
    agency_id = _require_agency(ctx)
    ip, ua = audit_logs_service.request_ip_ua(request)
    visit = await visits_service.end_visit(
        session, visit_id=visit_id, agency_id=agency_id, payload=payload
    )
    await session.commit()
    await session.refresh(visit)
    # Fan-out VISIT_ENDED to patient + guardians.
    await notif_integrations.notify_visit_ended(
        background_tasks,
        session,
        actor_user_id=ctx.user_id,
        actor_agency_id=agency_id,
        actor_role=ctx.role,
        visit_id=visit.id,
        agency_id=agency_id,
    )
    # Audit
    try:
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.UPDATE,
            entity_type="VISIT",
            entity_id=visit.id,
            new_data={"event": "EVV_END", "end_time_set": True},
            ip_address=ip,
            user_agent=ua,
        )
    except Exception:
        pass
    await session.commit()
    return _to_response(visit, with_relations=True)


@router.patch(
    "/{visit_id}/transition",
    response_model=VisitResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN, UserRole.STAFF))],
)
async def transition_visit_endpoint(
    visit_id: uuid.UUID,
    payload: VisitStatusTransitionRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> VisitResponse:
    """Walk the visit through the 5-state lifecycle with spec gates.

    `IN_PROGRESS → AWAITING_SIGNATURE` requires all activities resolved
    AND `billing_confirmed_at` set. `AWAITING_SIGNATURE → COMPLETED`
    requires an `AppointmentSignature` row.
    """
    agency_id = _require_agency(ctx)
    ip, ua = audit_logs_service.request_ip_ua(request)
    visit = await visits_service.transition_visit_status(
        session, visit_id=visit_id, agency_id=agency_id, payload=payload
    )
    await session.commit()
    await session.refresh(visit)
    # Best-effort audit + side-effect notifications on the
    # transition-into-AWAITING_SIGNATURE edge.
    try:
        if payload.status == VisitStatus.AWAITING_SIGNATURE:
            await audit_logs_service.audit_log(
                session,
                agency_id=agency_id,
                actor_user_id=ctx.user_id,
                action=AuditAction.VISIT_SUBMITTED_FOR_SIGNATURE,
                entity_type="VISIT",
                entity_id=visit.id,
                new_data={"status": visit.status.value},
                ip_address=ip,
                user_agent=ua,
            )
            await session.commit()
        elif payload.status == VisitStatus.COMPLETED:
            await audit_logs_service.audit_log(
                session,
                agency_id=agency_id,
                actor_user_id=ctx.user_id,
                action=AuditAction.VISIT_COMPLETED,
                entity_type="VISIT",
                entity_id=visit.id,
                new_data={"status": visit.status.value},
                ip_address=ip,
                user_agent=ua,
            )
            await session.commit()
    except Exception:
        pass
    return _to_response(visit, with_relations=True)


# --------------------------------------------------------------------------
# Billing confirmation (spec §6)
# --------------------------------------------------------------------------
@router.post(
    "/{visit_id}/confirm-billing",
    response_model=VisitResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN, UserRole.STAFF))],
)
async def confirm_billing_endpoint(
    visit_id: uuid.UUID,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> VisitResponse:
    """Caregiver ticks "I confirm the visit and billing information is correct".

    Spec §6 — required before the `IN_PROGRESS → AWAITING_SIGNATURE`
    transition. Idempotent.
    """
    agency_id = _require_agency(ctx)
    ip, ua = audit_logs_service.request_ip_ua(request)
    visit = await visits_service.confirm_billing(
        session,
        visit_id=visit_id,
        agency_id=agency_id,
        payload=VisitConfirmBillingRequest(),
        confirmed_by_user_id=ctx.user_id,
    )
    await session.commit()
    await session.refresh(visit)
    # Best-effort audit.
    try:
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.BILLING_CONFIRMED,
            entity_type="VISIT",
            entity_id=visit.id,
            new_data={"billing_confirmed_at": visit.billing_confirmed_at.isoformat()}
            if visit.billing_confirmed_at is not None
            else {},
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    return _to_response(visit, with_relations=True)


# --------------------------------------------------------------------------
# Live location sharing (EVV)
# --------------------------------------------------------------------------
@router.post(
    "/{visit_id}/start-location-sharing",
    response_model=VisitResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN, UserRole.STAFF))],
)
async def start_location_sharing_endpoint(
    visit_id: uuid.UUID,
    payload: VisitStartLocationSharingRequest,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> VisitResponse:
    """Opt in to live GPS sharing for an active visit."""
    agency_id = _require_agency(ctx)
    visit = await visits_service.start_location_sharing(
        session,
        visit_id=visit_id,
        agency_id=agency_id,
        initial_lat=payload.initial_lat,
        initial_lng=payload.initial_lng,
        initial_accuracy_m=payload.initial_accuracy_m,
    )
    await session.commit()
    await session.refresh(visit)
    return _to_response(visit, with_relations=True)


@router.post(
    "/{visit_id}/location-ping",
    response_model=VisitResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN, UserRole.STAFF))],
)
async def record_location_ping_endpoint(
    visit_id: uuid.UUID,
    payload: VisitLocationPingRequest,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> VisitResponse:
    """Persist the most recent GPS ping for an actively sharing visit."""
    agency_id = _require_agency(ctx)
    visit = await visits_service.record_location_ping(
        session,
        visit_id=visit_id,
        agency_id=agency_id,
        payload=payload,
    )
    await session.commit()
    await session.refresh(visit)
    return _to_response(visit, with_relations=True)


@router.post(
    "/{visit_id}/stop-location-sharing",
    response_model=VisitResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN, UserRole.STAFF))],
)
async def stop_location_sharing_endpoint(
    visit_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> VisitResponse:
    """Opt out of live GPS sharing while retaining the last known position."""
    agency_id = _require_agency(ctx)
    visit = await visits_service.stop_location_sharing(
        session,
        visit_id=visit_id,
        agency_id=agency_id,
    )
    await session.commit()
    await session.refresh(visit)
    return _to_response(visit, with_relations=True)


# --------------------------------------------------------------------------
# Visit activities (spec §5)
# --------------------------------------------------------------------------
@router.get(
    "/{visit_id}/activities",
    response_model=list[VisitActivityDeliveryResponse],
)
async def list_visit_activities_endpoint(
    visit_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> list[VisitActivityDeliveryResponse]:
    agency_id = _require_agency(ctx)
    visit = await visits_service.get_visit(
        session, visit_id=visit_id, agency_id=agency_id, with_relations=True
    )
    _ensure_can_view_visit(ctx, visit.staff_id)
    items = await visits_service.list_visit_activities(
        session, visit_id=visit_id, agency_id=agency_id
    )
    return [VisitActivityDeliveryResponse.model_validate(i) for i in items]


@router.patch(
    "/{visit_id}/activities/{activity_id}",
    response_model=VisitActivityDeliveryResponse,
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN, UserRole.STAFF))],
)
async def update_visit_activity_endpoint(
    visit_id: uuid.UUID,
    activity_id: uuid.UUID,
    payload: VisitActivityUpdateRequest,
    request: Request,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> VisitActivityDeliveryResponse:
    """Record the per-visit delivery outcome (DONE / NOT_DONE / etc.)."""
    _require_agency(ctx)
    item = await visits_service.update_visit_activity(
        session,
        activity_id=activity_id,
        visit_id=visit_id,
        payload=payload,
        completed_by_user_id=ctx.user_id,
    )
    await session.commit()
    await session.refresh(item)
    # Best-effort audit.
    try:
        ip, ua = audit_logs_service.request_ip_ua(request)
        action = (
            AuditAction.ACTIVITY_MARKED_DONE
            if payload.status and payload.status.value == "DONE"
            else AuditAction.ACTIVITY_MARKED_NOT_DONE
        )
        await audit_logs_service.audit_log(
            session,
            agency_id=item.agency_id,
            actor_user_id=ctx.user_id,
            action=action,
            entity_type="VISIT_ACTIVITY_DELIVERY",
            entity_id=item.id,
            new_data={
                "visit_id": str(visit_id),
                "status": payload.status.value if payload.status else None,
            },
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    return VisitActivityDeliveryResponse.model_validate(item)


# --------------------------------------------------------------------------
# Notes
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
# Signature (spec §8 — patient / guardian sign)
# --------------------------------------------------------------------------
@router.post(
    "/{visit_id}/sign",
    response_model=AppointmentSignatureResponse,
    status_code=status.HTTP_201_CREATED,
)
async def sign_visit_endpoint(
    visit_id: uuid.UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    signature_image: Annotated[UploadFile, File(description="PNG/JPEG signature blob")],
    signer_display_name_override: Annotated[
        str | None, Form(description="Optional override for the rendered name")
    ] = None,
) -> AppointmentSignatureResponse:
    """File a signature on a visit (spec §8 — mandatory for COMPLETED).

    Multipart upload. Signer must be PATIENT or GUARDIAN (or AGENCY_ADMIN
    on their behalf — e.g. for phone-confirmed sign-offs). The
    `signer_display_name` is auto-rendered as `"J. Smith"` from the
    signer's `users.full_name`; the FE can pass an override if its UI
    formats differently.

    Idempotent at the visit level: a second POST overwrites the prior
    row (1:1 UNIQUE constraint), keeping the most recent intent for
    audit.
    """
    agency_id = _require_agency(ctx)

    # Signer-role enforcement: PATIENT, GUARDIAN, or AGENCY_ADMIN.
    if ctx.role not in {UserRole.PATIENT, UserRole.GUARDIAN, UserRole.AGENCY_ADMIN}:
        raise ForbiddenError(
            "Only PATIENT, GUARDIAN, or AGENCY_ADMIN may sign a visit."
        )

    # Read + validate the uploaded signature image.
    content = await signature_image.read()
    if len(content) == 0:
        raise ValidationError("Empty signature payload.", details={"field": "signature_image"})
    if len(content) > settings.SIGNATURE_MAX_BYTES:
        raise ValidationError(
            "Signature payload too large.",
            details={"max_bytes": settings.SIGNATURE_MAX_BYTES},
        )
    mime_type = (signature_image.content_type or "").lower()
    if mime_type not in {t.lower() for t in settings.SIGNATURE_ALLOWED_MIME_TYPES}:
        raise ValidationError(
            "Unsupported signature MIME type.",
            details={
                "allowed": settings.SIGNATURE_ALLOWED_MIME_TYPES,
                "got": mime_type,
            },
        )

    # Persist to local FS (S3 wiring deferred).
    signature_url = _save_signature_locally(
        visit_id, content=content, mime_type=mime_type
    )

    ip, ua = audit_logs_service.request_ip_ua(request)
    sig = await visits_service.sign_visit(
        session,
        visit_id=visit_id,
        agency_id=agency_id,
        signer_user_id=ctx.user_id,
        signer_role=ctx.role,
        signature_image_url=signature_url,
        signer_display_name_override=signer_display_name_override,
        ip_address=ip,
        user_agent=ua,
    )
    await session.commit()
    await session.refresh(sig)
    # Best-effort audit + staff notification.
    try:
        await audit_logs_service.audit_log(
            session,
            agency_id=agency_id,
            actor_user_id=ctx.user_id,
            action=AuditAction.VISIT_SIGNED,
            entity_type="APPOINTMENT_SIGNATURE",
            entity_id=sig.id,
            new_data={
                "visit_id": str(visit_id),
                "signer_role": sig.signer_role.value,
            },
            ip_address=ip,
            user_agent=ua,
        )
        await session.commit()
    except Exception:
        pass
    await notif_integrations.notify_visit_signed(
        background_tasks,
        session,
        actor_user_id=ctx.user_id,
        actor_agency_id=agency_id,
        actor_role=ctx.role,
        visit_id=visit_id,
        agency_id=agency_id,
    )
    return AppointmentSignatureResponse.model_validate(sig)


__all__ = ["router", "me_router"]


# --------------------------------------------------------------------------
# /me/visits — self-service date-window listing
# --------------------------------------------------------------------------
# Same pattern as /me/appointments/calendar (see appointments/router.py):
#   - role-gated to PATIENT / STAFF / GUARDIAN
#   - caller identity resolved from `ctx.user_id` (no PII in the URL)
#   - no pagination; date window only
#   - eager-loads staff + parent appointment so the FE renders each
#     visit card without N+1 round trips
#
# AGENCY_ADMIN keeps using /visits for cross-user lookups.
# --------------------------------------------------------------------------


me_router = APIRouter(prefix="/me/visits", tags=["visits"])


def _visit_to_summary(visit: object) -> VisitSummaryResponse:
    """Project a `Visit` ORM row to `VisitSummaryResponse`, including
    the joined fields the FE renders in calendar cards.
    """
    staff_name: str | None = None
    try:
        staff = getattr(visit, "staff", None)
        if staff is not None:
            user = getattr(staff, "user", None)
            if user is not None:
                staff_name = getattr(user, "full_name", None)
    except Exception:
        staff_name = None

    scheduled_start = None
    try:
        appt = getattr(visit, "appointment", None)
        if appt is not None:
            scheduled_start = getattr(appt, "scheduled_start", None)
    except Exception:
        scheduled_start = None

    return VisitSummaryResponse(
        id=visit.id,
        appointment_id=visit.appointment_id,
        agency_id=visit.agency_id,
        staff_id=visit.staff_id,
        status=visit.status,
        billing_confirmed_at=getattr(visit, "billing_confirmed_at", None),
        live_lat=getattr(visit, "live_lat", None),
        live_lng=getattr(visit, "live_lng", None),
        live_ping_at=getattr(visit, "live_ping_at", None),
        live_accuracy_m=getattr(visit, "live_accuracy_m", None),
        sharing_location=getattr(visit, "sharing_location", False),
        created_at=visit.created_at,
        updated_at=visit.updated_at,
        staff_name=staff_name,
        patient_name=None,
        service_item_count=0,
        duration_label=None,
        scheduled_start=scheduled_start,
    )


@me_router.get(
    "",
    response_model=list[VisitSummaryResponse],
    responses=standard_responses(include=[401, 403]),
    summary="Caller's visits in a date window (no pagination)",
    description=(
        "Self-service. Returns every visit for the caller within "
        "`[date_from, date_to]` (both inclusive, calendar dates in "
        "UTC), sorted ascending by the parent appointment's "
        "`scheduled_start`. No pagination — the FE calendar typically "
        "wants every visit in a 1-3 month window.\n\n"
        "Role scope:\n"
        "  - **PATIENT**  — visits whose appointment.patient_id is the caller\n"
        "  - **STAFF**    — visits where `staff_id` is the caller's staff profile\n"
        "  - **GUARDIAN** — visits for every patient the caller is currently linked to\n\n"
        "Defaults to a ±30 day window around today if no dates are given."
    ),
    dependencies=[
        Depends(
            require_role(
                UserRole.PATIENT, UserRole.STAFF, UserRole.GUARDIAN
            )
        )
    ],
)
async def my_visits_endpoint(
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    date_from: date | None = Query(
        default=None,
        description="Inclusive start date (YYYY-MM-DD). Default: today - 30 days.",
    ),
    date_to: date | None = Query(
        default=None,
        description="Inclusive end date (YYYY-MM-DD). Default: today + 60 days.",
    ),
    status_filter: VisitStatus | None = Query(
        default=None, alias="status"
    ),
) -> list[VisitSummaryResponse]:
    from src.modules.patients import service as patients_service

    agency_id = _require_agency(ctx)
    today = utc_now().date()
    effective_from = date_from or (today - timedelta(days=30))
    effective_to = date_to or (today + timedelta(days=60))
    if effective_to < effective_from:
        raise ValidationError(
            "date_to must be on or after date_from.",
            details={
                "date_from": effective_from.isoformat(),
                "date_to": effective_to.isoformat(),
            },
        )

    patient_id: uuid.UUID | None = None
    staff_id: uuid.UUID | None = None

    if ctx.role == UserRole.PATIENT:
        patient = await patients_service.get_patient_by_user_id(
            session, user_id=ctx.user_id, agency_id=agency_id
        )
        patient_id = patient.id
    elif ctx.role == UserRole.STAFF:
        from src.modules.staff import service as staff_service

        staff = await staff_service.get_staff_by_user_id(
            session, user_id=ctx.user_id, agency_id=agency_id
        )
        staff_id = staff.id
    elif ctx.role == UserRole.GUARDIAN:
        guardian = await patients_service.get_guardian_by_user_id(
            session, user_id=ctx.user_id, agency_id=agency_id
        )
        patient_ids = await patients_service.list_guardian_patient_ids(
            session, guardian_id=guardian.id, agency_id=agency_id
        )
        if not patient_ids:
            return []
        merged: list[VisitSummaryResponse] = []
        for pid in patient_ids:
            rows = await visits_service.list_visits_in_window(
                session,
                agency_id=agency_id,
                date_from=effective_from,
                date_to=effective_to,
                patient_id=pid,
                status_filter=status_filter,
            )
            merged.extend(_visit_to_summary(r) for r in rows)
        merged.sort(
            key=lambda v: v.scheduled_start or v.created_at
        )
        return merged

    rows = await visits_service.list_visits_in_window(
        session,
        agency_id=agency_id,
        date_from=effective_from,
        date_to=effective_to,
        patient_id=patient_id,
        staff_id=staff_id,
        status_filter=status_filter,
    )
    return [_visit_to_summary(r) for r in rows]