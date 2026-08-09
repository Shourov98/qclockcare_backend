"""SUPER_ADMIN platform overview endpoints.

This surface is intentionally global (not agency-scoped). It powers the
separate `/super-admin/*` dashboard and aggregates platform-wide counts
without requiring a tenant context.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.agencies.models import Agency
from src.modules.appointments.models import Appointment
from src.modules.identity.dependencies import get_session_with_auth, require_role
from src.modules.identity.models import User
from src.modules.patients.models import PatientProfile
from src.modules.staff.models import StaffProfile
from src.modules.visits.models import Visit
from src.shared.domain.enums import (
    AgencyStatus,
    AgencySubscriptionPlan,
    UserRole,
    UserStatus,
    VisitStatus,
)
from src.shared.schemas.docs import standard_responses

router = APIRouter(prefix="/admin/platform", tags=["admin-platform"])
_SUPER_ADMIN_ONLY = [Depends(require_role(UserRole.SUPER_ADMIN))]


class PlatformRecentAgencyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    status: AgencyStatus
    subscription_plan: AgencySubscriptionPlan
    created_at: datetime


class PlatformDashboardSummaryResponse(BaseModel):
    """Global operational summary shown on the Super Admin home page."""

    total_agencies: int
    active_agencies: int
    trial_agencies: int
    suspended_agencies: int
    churned_agencies: int
    total_users: int
    active_users: int
    total_staff: int
    total_patients: int
    total_appointments: int
    total_visits: int
    live_visits: int
    monthly_recurring_revenue_cents: int
    agencies_by_plan: dict[str, int]
    recent_agencies: list[PlatformRecentAgencyResponse]


async def _scalar_count(session: AsyncSession, statement: object) -> int:
    return int((await session.execute(statement)).scalar_one())


@router.get(
    "/summary",
    response_model=PlatformDashboardSummaryResponse,
    dependencies=_SUPER_ADMIN_ONLY,
    responses=standard_responses(include=[401, 403]),
)
async def get_platform_summary_endpoint(
    session: Annotated[AsyncSession, Depends(get_session_with_auth)],
) -> PlatformDashboardSummaryResponse:
    """Return platform-wide agency, user, care, EVV, and revenue metrics."""

    agency_totals_stmt = select(
        func.count(Agency.id),
        func.sum(case((Agency.status == AgencyStatus.ACTIVE, 1), else_=0)),
        func.sum(case((Agency.status == AgencyStatus.TRIAL, 1), else_=0)),
        func.sum(case((Agency.status == AgencyStatus.SUSPENDED, 1), else_=0)),
        func.sum(case((Agency.status == AgencyStatus.CHURNED, 1), else_=0)),
        func.coalesce(
            func.sum(
                case(
                    (
                        Agency.status == AgencyStatus.ACTIVE,
                        Agency.subscription_price_cents,
                    ),
                    else_=0,
                )
            ),
            0,
        ),
    ).where(Agency.deleted_at.is_(None))
    agency_row = (await session.execute(agency_totals_stmt)).one()

    plan_rows = (
        await session.execute(
            select(Agency.subscription_plan, func.count(Agency.id))
            .where(Agency.deleted_at.is_(None))
            .group_by(Agency.subscription_plan)
        )
    ).all()
    agencies_by_plan = {plan.value: int(count) for plan, count in plan_rows}
    for plan in AgencySubscriptionPlan:
        agencies_by_plan.setdefault(plan.value, 0)

    total_users = await _scalar_count(
        session,
        select(func.count(User.id)).where(
            User.deleted_at.is_(None), User.status != UserStatus.ARCHIVED
        ),
    )
    active_users = await _scalar_count(
        session,
        select(func.count(User.id)).where(
            User.deleted_at.is_(None), User.status == UserStatus.ACTIVE
        ),
    )
    total_staff = await _scalar_count(
        session,
        select(func.count(StaffProfile.id)).join(
            Agency, Agency.id == StaffProfile.agency_id
        ).where(Agency.deleted_at.is_(None)),
    )
    total_patients = await _scalar_count(
        session,
        select(func.count(PatientProfile.id)).join(
            Agency, Agency.id == PatientProfile.agency_id
        ).where(Agency.deleted_at.is_(None)),
    )
    total_appointments = await _scalar_count(
        session,
        select(func.count(Appointment.id)).join(
            Agency, Agency.id == Appointment.agency_id
        ).where(Agency.deleted_at.is_(None)),
    )
    total_visits = await _scalar_count(
        session,
        select(func.count(Visit.id)).join(Agency, Agency.id == Visit.agency_id).where(
            Agency.deleted_at.is_(None)
        ),
    )
    live_visits = await _scalar_count(
        session,
        select(func.count(Visit.id)).join(Agency, Agency.id == Visit.agency_id).where(
            Agency.deleted_at.is_(None),
            Visit.sharing_location.is_(True),
            Visit.status.in_([VisitStatus.CHECKED_IN, VisitStatus.IN_PROGRESS]),
        ),
    )

    recent_rows = (
        (
            await session.execute(
                select(Agency)
                .where(Agency.deleted_at.is_(None))
                .order_by(Agency.created_at.desc(), Agency.id)
                .limit(5)
            )
        )
        .scalars()
        .all()
    )

    return PlatformDashboardSummaryResponse(
        total_agencies=int(agency_row[0] or 0),
        active_agencies=int(agency_row[1] or 0),
        trial_agencies=int(agency_row[2] or 0),
        suspended_agencies=int(agency_row[3] or 0),
        churned_agencies=int(agency_row[4] or 0),
        total_users=total_users,
        active_users=active_users,
        total_staff=total_staff,
        total_patients=total_patients,
        total_appointments=total_appointments,
        total_visits=total_visits,
        live_visits=live_visits,
        monthly_recurring_revenue_cents=int(agency_row[5] or 0),
        agencies_by_plan=agencies_by_plan,
        recent_agencies=[
            PlatformRecentAgencyResponse.model_validate(agency)
            for agency in recent_rows
        ],
    )


__all__ = ["router"]
