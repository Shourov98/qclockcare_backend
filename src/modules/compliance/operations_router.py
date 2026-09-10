"""Agency operational compliance endpoints.

Unlike `/admin/compliance`, which is a platform-admin surface, these routes
are tenant scoped and power the agency dashboard and member reporting flows.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.compliance import operations_service as service
from src.modules.compliance.models import AgencyComplianceReport, ServiceAuthorization, SupervisoryVisit
from src.modules.compliance.operations_schemas import (
    ComplianceOverviewResponse, ComplianceReportCreateRequest, ComplianceReportResolveRequest,
    ComplianceReportResponse, ServiceAuthorizationCreateRequest, ServiceAuthorizationResponse,
    ServiceAuthorizationUsageRequest, SupervisoryVisitCompleteRequest, SupervisoryVisitCreateRequest,
    SupervisoryVisitResponse,
)
from src.modules.identity.dependencies import CurrentAuth, get_session_with_auth
from src.shared.domain.enums import UserRole

router = APIRouter(prefix="/compliance", tags=["agency-compliance"])


async def _authorization_response(session: AsyncSession, item: ServiceAuthorization) -> ServiceAuthorizationResponse:
    names = await service.names(session, patient_id=item.patient_id)
    return ServiceAuthorizationResponse(
        id=item.id, patient_id=item.patient_id, patient_name=names["patient_name"] or "Unknown patient",
        program_type=item.program_type, service_name=item.service_name, authorization_number=item.authorization_number,
        starts_on=item.starts_on, ends_on=item.ends_on, authorized_units=float(item.authorized_units), used_units=float(item.used_units),
        remaining_units=max(float(item.authorized_units) - float(item.used_units), 0), unit_label=item.unit_label,
        status=item.status, notes=item.notes, created_at=item.created_at,
    )


async def _visit_response(session: AsyncSession, item: SupervisoryVisit) -> SupervisoryVisitResponse:
    staff = await service.names(session, staff_id=item.staff_id)
    patient = await service.names(session, patient_id=item.patient_id) if item.patient_id else {"patient_name": None}
    supervisor = await service.names(session, user_id=item.supervisor_user_id)
    return SupervisoryVisitResponse(
        id=item.id, staff_id=item.staff_id, staff_name=staff["staff_name"] or "Unknown staff", patient_id=item.patient_id,
        patient_name=patient["patient_name"], supervisor_name=supervisor["user_name"] or "Agency administrator",
        scheduled_at=item.scheduled_at, completed_at=item.completed_at, status=item.status, objectives=item.objectives,
        findings=item.findings, acknowledged_at=item.acknowledged_at,
    )


async def _report_response(session: AsyncSession, item: AgencyComplianceReport) -> ComplianceReportResponse:
    reporter = await service.names(session, user_id=item.reporter_user_id)
    patient = await service.names(session, patient_id=item.patient_id) if item.patient_id else {"patient_name": None}
    return ComplianceReportResponse(
        id=item.id, reporter_name=reporter["user_name"] or "Unknown reporter", patient_id=item.patient_id,
        patient_name=patient["patient_name"], category=item.category, severity=item.severity, status=item.status,
        title=item.title, description=item.description, resolution_note=item.resolution_note,
        created_at=item.created_at, resolved_at=item.resolved_at,
    )


@router.get("/overview", response_model=ComplianceOverviewResponse)
async def get_overview(ctx: CurrentAuth, session: Annotated[AsyncSession, Depends(get_session_with_auth)]) -> ComplianceOverviewResponse:
    return ComplianceOverviewResponse(**await service.overview(session, ctx=ctx))


@router.get("/authorizations", response_model=list[ServiceAuthorizationResponse])
async def get_authorizations(ctx: CurrentAuth, session: Annotated[AsyncSession, Depends(get_session_with_auth)]) -> list[ServiceAuthorizationResponse]:
    return [await _authorization_response(session, item) for item in await service.list_authorizations(session, ctx=ctx)]


@router.post("/authorizations", response_model=ServiceAuthorizationResponse, status_code=status.HTTP_201_CREATED)
async def create_authorization(payload: ServiceAuthorizationCreateRequest, ctx: CurrentAuth, session: Annotated[AsyncSession, Depends(get_session_with_auth)]) -> ServiceAuthorizationResponse:
    return await _authorization_response(session, await service.create_authorization(session, ctx=ctx, **payload.model_dump()))


@router.post("/authorizations/{authorization_id}/usage", response_model=ServiceAuthorizationResponse)
async def record_authorization_usage(authorization_id: uuid.UUID, payload: ServiceAuthorizationUsageRequest, ctx: CurrentAuth, session: Annotated[AsyncSession, Depends(get_session_with_auth)]) -> ServiceAuthorizationResponse:
    return await _authorization_response(session, await service.add_authorization_usage(session, ctx=ctx, authorization_id=authorization_id, units=payload.units))


@router.get("/supervisory-visits", response_model=list[SupervisoryVisitResponse])
async def get_supervisory_visits(ctx: CurrentAuth, session: Annotated[AsyncSession, Depends(get_session_with_auth)]) -> list[SupervisoryVisitResponse]:
    return [await _visit_response(session, item) for item in await service.list_supervisory_visits(session, ctx=ctx)]


@router.post("/supervisory-visits", response_model=SupervisoryVisitResponse, status_code=status.HTTP_201_CREATED)
async def create_supervisory_visit(payload: SupervisoryVisitCreateRequest, ctx: CurrentAuth, session: Annotated[AsyncSession, Depends(get_session_with_auth)]) -> SupervisoryVisitResponse:
    return await _visit_response(session, await service.create_supervisory_visit(session, ctx=ctx, **payload.model_dump()))


@router.post("/supervisory-visits/{visit_id}/complete", response_model=SupervisoryVisitResponse)
async def complete_supervisory_visit(visit_id: uuid.UUID, payload: SupervisoryVisitCompleteRequest, ctx: CurrentAuth, session: Annotated[AsyncSession, Depends(get_session_with_auth)]) -> SupervisoryVisitResponse:
    return await _visit_response(session, await service.complete_supervisory_visit(session, ctx=ctx, visit_id=visit_id, findings=payload.findings))


@router.post("/supervisory-visits/{visit_id}/acknowledge", response_model=SupervisoryVisitResponse)
async def acknowledge_supervisory_visit(visit_id: uuid.UUID, ctx: CurrentAuth, session: Annotated[AsyncSession, Depends(get_session_with_auth)]) -> SupervisoryVisitResponse:
    return await _visit_response(session, await service.acknowledge_supervisory_visit(session, ctx=ctx, visit_id=visit_id))


@router.get("/reports", response_model=list[ComplianceReportResponse])
async def get_reports(ctx: CurrentAuth, session: Annotated[AsyncSession, Depends(get_session_with_auth)]) -> list[ComplianceReportResponse]:
    return [await _report_response(session, item) for item in await service.list_reports(session, ctx=ctx)]


@router.post("/reports", response_model=ComplianceReportResponse, status_code=status.HTTP_201_CREATED)
async def create_report(payload: ComplianceReportCreateRequest, ctx: CurrentAuth, session: Annotated[AsyncSession, Depends(get_session_with_auth)]) -> ComplianceReportResponse:
    return await _report_response(session, await service.create_report(session, ctx=ctx, **payload.model_dump()))


@router.post("/reports/{report_id}/resolve", response_model=ComplianceReportResponse)
async def resolve_report(report_id: uuid.UUID, payload: ComplianceReportResolveRequest, ctx: CurrentAuth, session: Annotated[AsyncSession, Depends(get_session_with_auth)]) -> ComplianceReportResponse:
    return await _report_response(session, await service.resolve_report(session, ctx=ctx, report_id=report_id, note=payload.resolution_note, dismiss=payload.dismiss))
