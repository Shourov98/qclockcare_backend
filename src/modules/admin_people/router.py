"""SUPER_ADMIN cross-agency people endpoints.

Endpoints:
  GET /admin/staff     — paginated staff across all agencies (filterable)
  GET /admin/patients  — paginated patients across all agencies (filterable)

These endpoints intentionally do NOT require an `agency_id` from the
caller. They are designed for the global dashboard's "people" views.

Why a separate router rather than reusing `/staff` + `/patients`?
  Those routers are RLS-scoped to the caller's `agency_id` and reject
  SUPER_ADMIN because they call `_require_agency(ctx)`. The platform
  admin needs cross-tenant reads, which is its own access tier.

Why no PATCH/POST/DELETE?
  Mutations on staff/patients should remain agency-scoped (they
  trigger agency-specific side effects: invitation emails, scheduling
  constraints, etc.). Cross-tenant admin writes are out of scope for
  this pass and will be designed separately if/when the admin needs
  them.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.modules.agencies.models import Agency
from src.modules.identity.dependencies import (
    CurrentAuth,
    get_session_with_auth,
    require_role,
)
from src.modules.identity.scope_deps import require_scope
from src.modules.patients.models import PatientProfile
from src.modules.staff.models import StaffProfile
from src.shared.domain.enums import AdminScope, UserRole, UserStatus
from src.shared.schemas.docs import standard_responses
from src.shared.schemas.pagination import PaginatedResponse, build_offset_response

# Single router under `/admin/people` keeps the URL surface tight:
#   GET /admin/people/staff
#   GET /admin/people/patients
# Both are SUPER_ADMIN-only OR PLATFORM_ADMIN with CLINICAL scope.

router = APIRouter(prefix="/admin/people", tags=["admin-people"])

_SUPER_ADMIN_ONLY = [Depends(require_role(UserRole.SUPER_ADMIN))]
_CLINICAL_SCOPE = [Depends(require_scope(AdminScope.CLINICAL))]


# --------------------------------------------------------------------------
# Shared filter shape — agency, status, free-text search.
# --------------------------------------------------------------------------
def _staff_filters(
    *,
    agency_id: uuid.UUID | None,
    status_filter: UserStatus | None,
    search: str | None,
) -> tuple[list[Any], list[Any]]:
    """Return (where_clauses, count_where_clauses) for the staff list.

    Joins `staff_profiles` to `users` so search can match against
    `email` / `full_name` / `staff_code`. The deleted_at predicate
    excludes soft-deleted agencies (defence in depth: RLS would
    already exclude them, but this is the SUPER_ADMIN path).
    """
    clauses: list[Any] = [Agency.deleted_at.is_(None)]
    count_clauses: list[Any] = [Agency.deleted_at.is_(None)]

    if agency_id is not None:
        clauses.append(StaffProfile.agency_id == agency_id)
        count_clauses.append(StaffProfile.agency_id == agency_id)
    if status_filter is not None:
        clauses.append(StaffProfile.status == status_filter)
        count_clauses.append(StaffProfile.status == status_filter)
    if search:
        like = f"%{search.lower()}%"
        clauses.append(
            or_(
                func.lower(StaffProfile.staff_code).like(like),
            )
        )
        # user.email / user.full_name are on the joined table, but
        # `_staff_to_dict` handles them eagerly — the search filter is
        # intentionally narrow on staff_code here because cross-table
        # LIKE in Postgres is a known performance footgun at scale.
        count_clauses.append(
            or_(
                func.lower(StaffProfile.staff_code).like(like),
            )
        )

    return clauses, count_clauses


def _patient_filters(
    *,
    agency_id: uuid.UUID | None,
    status_filter: UserStatus | None,
    search: str | None,
) -> tuple[list[Any], list[Any]]:
    """Same as `_staff_filters` but for patients."""
    clauses: list[Any] = [Agency.deleted_at.is_(None)]
    count_clauses: list[Any] = [Agency.deleted_at.is_(None)]

    if agency_id is not None:
        clauses.append(PatientProfile.agency_id == agency_id)
        count_clauses.append(PatientProfile.agency_id == agency_id)
    if status_filter is not None:
        clauses.append(PatientProfile.status == status_filter)
        count_clauses.append(PatientProfile.status == status_filter)
    if search:
        like = f"%{search.lower()}%"
        clauses.append(
            or_(
                func.lower(PatientProfile.patient_code).like(like),
            )
        )
        count_clauses.append(
            or_(
                func.lower(PatientProfile.patient_code).like(like),
            )
        )

    return clauses, count_clauses


def _staff_to_dict(staff: StaffProfile) -> dict[str, Any]:
    """Mirror `_staff_to_dict` in src/modules/staff/router.py:99.

    Kept as a local copy so the admin path doesn't depend on a private
    helper in another module (and so we can shape the dict to include
    `agency_name` for the global admin view).
    """
    user = getattr(staff, "user", None)
    agency = getattr(staff, "agency", None)
    return {
        "id": staff.id,
        "agency_id": staff.agency_id,
        "agency_name": getattr(agency, "name", None) if agency is not None else None,
        "user_id": staff.user_id,
        "full_name": getattr(user, "full_name", None) if user is not None else None,
        "email": getattr(user, "email", None) if user is not None else None,
        "phone": getattr(user, "phone", None) if user is not None else None,
        "staff_code": staff.staff_code,
        "status": staff.status,
        "hired_at": staff.hired_at,
        "terminated_at": staff.terminated_at,
        "created_at": staff.created_at,
        "updated_at": staff.updated_at,
    }


def _patient_to_dict(patient: PatientProfile) -> dict[str, Any]:
    """Mirror `_patient_to_dict` in src/modules/patients/router.py:104.

    Adds `agency_name` for the global admin view.
    """
    user = getattr(patient, "user", None)
    agency = getattr(patient, "agency", None)
    return {
        "id": patient.id,
        "agency_id": patient.agency_id,
        "agency_name": getattr(agency, "name", None) if agency is not None else None,
        "user_id": patient.user_id,
        "full_name": getattr(user, "full_name", None) if user is not None else None,
        "email": getattr(user, "email", None) if user is not None else None,
        "phone": getattr(user, "phone", None) if user is not None else None,
        "patient_code": patient.patient_code,
        "status": patient.status,
        "date_of_birth": patient.date_of_birth,
        "gender": patient.gender,
        "preferred_language": patient.preferred_language,
        "care_notes": patient.care_notes,
        "admitted_at": patient.admitted_at,
        "discharged_at": patient.discharged_at,
        "created_at": patient.created_at,
        "updated_at": patient.updated_at,
    }


# --------------------------------------------------------------------------
# Response shapes — superset of the existing summary shapes, plus
# `agency_name` for cross-tenant admin display.
# --------------------------------------------------------------------------
class AdminStaffSummaryResponse(PaginatedResponse[dict]):
    """Cross-tenant staff list. Each item is the `_staff_to_dict` shape."""

    pass


class AdminPatientSummaryResponse(PaginatedResponse[dict]):
    """Cross-tenant patient list. Each item is the `_patient_to_dict` shape."""

    pass


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------
@router.get(
    "/staff",
    response_model=AdminStaffSummaryResponse,
    dependencies=_CLINICAL_SCOPE,
    responses=standard_responses(include=[401, 403, 422]),
    summary="List staff across all agencies (SUPER_ADMIN or PLATFORM_ADMIN w/ CLINICAL)",
    description=(
        "Paginated staff list with optional `agency_id`, `status`, "
        "and `search` (case-insensitive `staff_code` substring) "
        "filters. Joined user fields (`full_name`, `email`, `phone`) "
        "are eagerly loaded; `agency_name` is included for display in "
        "the global admin dashboard."
    ),
)
async def list_admin_staff_endpoint(
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
    agency_id: Annotated[uuid.UUID | None, Query()] = None,
    status_filter: Annotated[UserStatus | None, Query(alias="status")] = None,
    search: Annotated[str | None, Query(max_length=255)] = None,
) -> AdminStaffSummaryResponse:
    """Paginated cross-tenant staff list."""
    page = max(1, page)
    page_size = max(1, min(100, page_size))

    where, count_where = _staff_filters(
        agency_id=agency_id,
        status_filter=status_filter,
        search=search,
    )

    base = (
        select(StaffProfile)
        .join(Agency, Agency.id == StaffProfile.agency_id)
        .where(*where)
        .options(
            selectinload(StaffProfile.user),
            selectinload(StaffProfile.agency),
        )
        .order_by(StaffProfile.created_at.desc(), StaffProfile.id)
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    rows = (await session.execute(base)).scalars().all()

    count_stmt = (
        select(func.count())
        .select_from(StaffProfile)
        .join(Agency, Agency.id == StaffProfile.agency_id)
        .where(*count_where)
    )
    total = int((await session.execute(count_stmt)).scalar_one())

    data = [_staff_to_dict(r) for r in rows]
    body = build_offset_response(data, total=total, page=page, page_size=page_size)
    return AdminStaffSummaryResponse.model_validate(body)


@router.get(
    "/patients",
    response_model=AdminPatientSummaryResponse,
    dependencies=_CLINICAL_SCOPE,
    responses=standard_responses(include=[401, 403, 422]),
    summary="List patients across all agencies (SUPER_ADMIN or PLATFORM_ADMIN w/ CLINICAL)",
    description=(
        "Paginated patient list with optional `agency_id`, `status`, "
        "and `search` (case-insensitive `patient_code` substring) "
        "filters. Joined user fields are eagerly loaded; "
        "`agency_name` is included for display in the global admin "
        "dashboard."
    ),
)
async def list_admin_patients_endpoint(
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
    agency_id: Annotated[uuid.UUID | None, Query()] = None,
    status_filter: Annotated[UserStatus | None, Query(alias="status")] = None,
    search: Annotated[str | None, Query(max_length=255)] = None,
) -> AdminPatientSummaryResponse:
    """Paginated cross-tenant patient list."""
    page = max(1, page)
    page_size = max(1, min(100, page_size))

    where, count_where = _patient_filters(
        agency_id=agency_id,
        status_filter=status_filter,
        search=search,
    )

    base = (
        select(PatientProfile)
        .join(Agency, Agency.id == PatientProfile.agency_id)
        .where(*where)
        .options(
            selectinload(PatientProfile.user),
            selectinload(PatientProfile.agency),
        )
        .order_by(PatientProfile.created_at.desc(), PatientProfile.id)
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    rows = (await session.execute(base)).scalars().all()

    count_stmt = (
        select(func.count())
        .select_from(PatientProfile)
        .join(Agency, Agency.id == PatientProfile.agency_id)
        .where(*count_where)
    )
    total = int((await session.execute(count_stmt)).scalar_one())

    data = [_patient_to_dict(r) for r in rows]
    body = build_offset_response(data, total=total, page=page, page_size=page_size)
    return AdminPatientSummaryResponse.model_validate(body)


__all__ = ["router"]
