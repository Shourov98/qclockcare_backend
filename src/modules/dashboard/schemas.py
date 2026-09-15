from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DashboardMonthPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    month: date
    paid_amount_cents: int = 0
    completed_visits: int = 0
    new_staff: int = 0
    new_clients: int = 0


class AuditReadinessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: int = Field(ge=0, le=100)
    evv_score: int = Field(ge=0, le=100)
    documentation_score: int = Field(ge=0, le=100)
    authorization_score: int = Field(ge=0, le=100)
    open_reports: int = 0
    expired_authorizations: int = 0


class AgencyDashboardOverviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active_clients: int
    active_staff: int
    visits_today: int
    sa_units_used: float
    sa_units_authorized: float
    recent_visits_count: int
    trends: list[DashboardMonthPoint]
    audit_readiness: AuditReadinessResponse


class SearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["patient", "staff", "appointment", "group_home"]
    id: UUID
    title: str
    subtitle: str | None = None
    href: str


class GlobalSearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    results: list[SearchResult]
