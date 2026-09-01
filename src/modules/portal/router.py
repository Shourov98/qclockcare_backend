"""Patient/Guardian portal router — `/portal/visits/...` + `/portal/compliance`.

All routes require authentication with role PATIENT or GUARDIAN.
Cross-agency / unlinked visits return 404 (not 403) to avoid leaking
visit existence to unrelated patients/guardians.

The portal is read-only — the state-mutating actions live on the
visits router:
  - `POST /visits/{id}/sign` — patient/guardian signs (spec §8)
  - `POST /visits/{id}/confirm-billing` — admin/staff only
  - `PATCH /visits/{id}/transition` — admin/staff only
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.logging import get_logger
from src.modules.identity.dependencies import (
    CurrentAuth,
    get_session_with_auth,
)
from src.modules.portal import service as portal_service
from src.modules.portal.schemas import (
    PortalComplianceResponse,
    PortalVisitListItem,
    PortalVisitResponse,
)
from src.shared.utils.labels import (
    duration_label,
    patient_initials,
    time_range_label,
    visit_date_label,
)

# Split into two routers so the FE can hit `/portal/compliance` without
# pulling visit-related OpenAPI tags. Both routers live under the
# `/portal` mount in `main.py`.
visits_router = APIRouter(prefix="/portal/visits", tags=["portal"])
compliance_router = APIRouter(prefix="/portal", tags=["portal-compliance"])

log = get_logger(__name__)


def _to_response(visit, *, with_relations: bool = False) -> PortalVisitResponse:
    """Build a `PortalVisitResponse` from an eager-loaded Visit.

    Mirrors the staff Visit Summary shape but trimmed for portal use:
    patient-friendly caregiver name, joined display labels, the EVV
    record (start + end), per-activity delivery status, notes, and the
    (required) signature when filed.
    """
    data: dict = {
        "id": visit.id,
        "appointment_id": visit.appointment_id,
        "agency_id": visit.agency_id,
        "staff_id": visit.staff_id,
        "status": visit.status,
        "billing_confirmed_at": getattr(visit, "billing_confirmed_at", None),
        "created_at": visit.created_at,
        "updated_at": visit.updated_at,
        "live_lat": getattr(visit, "live_lat", None),
        "live_lng": getattr(visit, "live_lng", None),
        "live_ping_at": getattr(visit, "live_ping_at", None),
        "sharing_location": getattr(visit, "sharing_location", False),
    }
    # ----- Joined caregiver + patient info -----
    # `load_visit_with_relations` eager-loads `staff.user`,
    # `appointment.patient.user`, and the nested children. Use the
    # try/except pattern so a lazy-load miss (e.g. an unrelated code
    # path calling this helper without the full eager load) doesn't
    # blow up — it just renders the joined field as None.
    try:
        staff = visit.staff
        if staff is not None:
            data["staff_code"] = getattr(staff, "staff_code", None)
            user = getattr(staff, "user", None)
            if user is not None:
                data["staff_name"] = getattr(user, "full_name", None)
                data["staff_phone"] = getattr(user, "phone", None)
    except Exception:
        pass
    try:
        appointment = visit.appointment
        if appointment is not None:
            data["location_label"] = getattr(appointment, "location", None)
            data["scheduled_start"] = getattr(appointment, "scheduled_start", None)
            data["scheduled_end"] = getattr(appointment, "scheduled_end", None)
            patient = getattr(appointment, "patient", None)
            if patient is not None:
                data["patient_code"] = getattr(patient, "patient_code", None)
                user = getattr(patient, "user", None)
                if user is not None:
                    data["patient_name"] = getattr(user, "full_name", None)
                    data["patient_initials"] = patient_initials(
                        getattr(user, "full_name", None)
                    )
    except Exception:
        pass
    # ----- Derived display labels from EVV start/end + appointment window -----
    evv = getattr(visit, "evv_record", None)
    if evv is not None:
        try:
            start_time = getattr(evv, "start_time", None)
            end_time = getattr(evv, "end_time", None)
            if start_time is not None and end_time is not None:
                secs = int((end_time - start_time).total_seconds())
                data["duration_seconds"] = secs
                data["duration_label"] = duration_label(secs)
                data["time_range_label"] = time_range_label(start_time, end_time)
                data["visit_date_label"] = visit_date_label(start_time)
        except Exception:
            pass

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
    return PortalVisitResponse.model_validate(data)


@visits_router.get("", response_model=list[PortalVisitListItem])
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
    out: list[PortalVisitListItem] = []
    for v in visits:
        appt = getattr(v, "appointment", None)
        scheduled_start = getattr(appt, "scheduled_start", None) if appt else None
        staff_name: str | None = None
        try:
            staff = getattr(v, "staff", None)
            if staff is not None:
                user = getattr(staff, "user", None)
                if user is not None:
                    staff_name = getattr(user, "full_name", None)
        except Exception:
            pass
        out.append(
            PortalVisitListItem.model_validate(
                {
                    "id": v.id,
                    "appointment_id": v.appointment_id,
                    "status": v.status,
                    "scheduled_start": scheduled_start,
                    "scheduled_end": getattr(appt, "scheduled_end", None) if appt else None,
                    "duration_label": None,  # populated when EVV ends
                    "staff_name": staff_name,
                    "created_at": v.created_at,
                }
            )
        )
    return out


@visits_router.get("/{visit_id}", response_model=PortalVisitResponse)
async def get_my_visit_endpoint(
    visit_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> PortalVisitResponse:
    """Single visit + nested activities / notes / signature / EVV."""
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


# --------------------------------------------------------------------------
# Compliance dashboard (`/portal/compliance`)
# --------------------------------------------------------------------------
@compliance_router.get(
    "/compliance",
    response_model=PortalComplianceResponse,
)
async def get_portal_compliance_endpoint(
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> PortalComplianceResponse:
    """Compliance dashboard rollup for the calling patient/guardian.

    Computed on read from live counts on `agency_documents`,
    `agency_licenses`, and `compliance_issues`. Powers the
    `farhan-salad-website/app/(dashboard)/compliance/page.tsx` hub.
    Auth: PATIENT or GUARDIAN (the service layer enforces the
    same 404-if-not-linked pattern as `/portal/visits`).
    """
    return await portal_service.get_portal_compliance(session, ctx=ctx)


__all__ = ["visits_router", "compliance_router"]