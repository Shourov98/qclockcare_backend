from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import ForbiddenError
from src.modules.dashboard import service
from src.modules.dashboard.schemas import AgencyDashboardOverviewResponse, GlobalSearchResponse
from src.modules.identity.dependencies import CurrentAuth, get_session_with_auth, require_role
from src.shared.domain.enums import UserRole

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _agency_id(ctx: CurrentAuth):
    if ctx.agency_id is None:
        raise ForbiddenError("An agency context is required.")
    return ctx.agency_id


@router.get("/overview", response_model=AgencyDashboardOverviewResponse, dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))])
async def get_overview(ctx: CurrentAuth, session: Annotated[AsyncSession, Depends(get_session_with_auth)], months: int = Query(default=6, ge=1, le=24)) -> AgencyDashboardOverviewResponse:
    return AgencyDashboardOverviewResponse(**await service.overview(session, agency_id=_agency_id(ctx), months=months))


@router.get("/search", response_model=GlobalSearchResponse, dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))])
async def global_search(ctx: CurrentAuth, session: Annotated[AsyncSession, Depends(get_session_with_auth)], q: str = Query(min_length=1, max_length=120), limit: int = Query(default=20, ge=1, le=50)) -> GlobalSearchResponse:
    return GlobalSearchResponse(query=q, results=await service.search(session, agency_id=_agency_id(ctx), query=q, limit=limit))
