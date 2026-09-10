"""Agency-scoped compliance operations and role-aware access checks."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import CrossAgencyAccessDeniedError, NotFoundError, ValidationError
from src.modules.compliance.models import AgencyComplianceReport, ServiceAuthorization, SupervisoryVisit
from src.modules.identity.dependencies import AuthContext
from src.modules.identity.models import User
from src.modules.patients.models import GuardianProfile, PatientGuardianRelationship, PatientProfile
from src.modules.staff.models import StaffProfile
from src.shared.domain.enums import UserRole
from src.shared.utils.datetime_utils import utc_now


def agency_id_for(ctx: AuthContext) -> uuid.UUID:
    if ctx.agency_id is None:
        raise CrossAgencyAccessDeniedError(message="An agency-scoped account is required.")
    return ctx.agency_id


def require_agency_admin(ctx: AuthContext) -> uuid.UUID:
    if ctx.role is not UserRole.AGENCY_ADMIN:
        raise CrossAgencyAccessDeniedError(message="Agency administrator access is required.")
    return agency_id_for(ctx)


async def _patient(session: AsyncSession, *, agency_id: uuid.UUID, patient_id: uuid.UUID) -> PatientProfile:
    item = (await session.execute(select(PatientProfile).where(PatientProfile.id == patient_id, PatientProfile.agency_id == agency_id, PatientProfile.deleted_at.is_(None)))).scalar_one_or_none()
    if item is None:
        raise NotFoundError(message="Patient not found.")
    return item


async def _validate_report_patient(session: AsyncSession, *, ctx: AuthContext, patient_id: uuid.UUID | None) -> None:
    if patient_id is None:
        return
    agency_id = agency_id_for(ctx)
    patient = await _patient(session, agency_id=agency_id, patient_id=patient_id)
    if ctx.role is UserRole.PATIENT and patient.user_id != ctx.user_id:
        raise CrossAgencyAccessDeniedError(message="Patients may only report concerns about themselves.")
    if ctx.role is UserRole.GUARDIAN:
        guardian = (await session.execute(select(GuardianProfile).where(GuardianProfile.agency_id == agency_id, GuardianProfile.user_id == ctx.user_id, GuardianProfile.deleted_at.is_(None)))).scalar_one_or_none()
        linked = None if guardian is None else (await session.execute(select(PatientGuardianRelationship.id).where(PatientGuardianRelationship.agency_id == agency_id, PatientGuardianRelationship.patient_id == patient_id, PatientGuardianRelationship.guardian_id == guardian.id, PatientGuardianRelationship.valid_until.is_(None)))).scalar_one_or_none()
        if linked is None:
            raise CrossAgencyAccessDeniedError(message="Guardians may only report concerns for linked patients.")


def _authorization_status(item: ServiceAuthorization) -> str:
    today = date.today()
    if item.status == "CANCELLED":
        return "CANCELLED"
    if item.used_units >= item.authorized_units:
        return "EXHAUSTED"
    if item.ends_on < today:
        return "EXPIRED"
    if item.ends_on <= today + timedelta(days=30):
        return "EXPIRING"
    return "ACTIVE"


async def list_authorizations(session: AsyncSession, *, ctx: AuthContext) -> list[ServiceAuthorization]:
    agency_id = agency_id_for(ctx)
    stmt = select(ServiceAuthorization).where(ServiceAuthorization.agency_id == agency_id, ServiceAuthorization.deleted_at.is_(None)).order_by(ServiceAuthorization.ends_on)
    if ctx.role is UserRole.PATIENT:
        patient = (await session.execute(select(PatientProfile).where(PatientProfile.agency_id == agency_id, PatientProfile.user_id == ctx.user_id, PatientProfile.deleted_at.is_(None)))).scalar_one_or_none()
        if patient is None:
            return []
        stmt = stmt.where(ServiceAuthorization.patient_id == patient.id)
    elif ctx.role is UserRole.GUARDIAN:
        stmt = stmt.join(PatientGuardianRelationship, PatientGuardianRelationship.patient_id == ServiceAuthorization.patient_id).join(GuardianProfile, GuardianProfile.id == PatientGuardianRelationship.guardian_id).where(GuardianProfile.user_id == ctx.user_id, PatientGuardianRelationship.valid_until.is_(None))
    return list((await session.execute(stmt)).scalars().all())


async def create_authorization(session: AsyncSession, *, ctx: AuthContext, **values: object) -> ServiceAuthorization:
    agency_id = require_agency_admin(ctx)
    await _patient(session, agency_id=agency_id, patient_id=values["patient_id"])
    item = ServiceAuthorization(agency_id=agency_id, **values)
    item.status = _authorization_status(item)
    session.add(item)
    await session.flush()
    return item


async def add_authorization_usage(session: AsyncSession, *, ctx: AuthContext, authorization_id: uuid.UUID, units: float) -> ServiceAuthorization:
    agency_id = require_agency_admin(ctx)
    item = (await session.execute(select(ServiceAuthorization).where(ServiceAuthorization.id == authorization_id, ServiceAuthorization.agency_id == agency_id, ServiceAuthorization.deleted_at.is_(None)))).scalar_one_or_none()
    if item is None:
        raise NotFoundError(message="Service authorization not found.")
    if item.status in {"CANCELLED", "EXPIRED"}:
        raise ValidationError(message="Usage cannot be recorded for an inactive authorization.")
    item.used_units += units
    item.status = _authorization_status(item)
    await session.flush()
    return item


async def list_supervisory_visits(session: AsyncSession, *, ctx: AuthContext) -> list[SupervisoryVisit]:
    agency_id = agency_id_for(ctx)
    stmt = select(SupervisoryVisit).where(SupervisoryVisit.agency_id == agency_id).order_by(SupervisoryVisit.scheduled_at.desc())
    if ctx.role is UserRole.STAFF:
        staff = (await session.execute(select(StaffProfile).where(StaffProfile.agency_id == agency_id, StaffProfile.user_id == ctx.user_id))).scalar_one_or_none()
        if staff is None:
            return []
        stmt = stmt.where(SupervisoryVisit.staff_id == staff.id)
    elif ctx.role is not UserRole.AGENCY_ADMIN:
        return []
    return list((await session.execute(stmt)).scalars().all())


async def create_supervisory_visit(session: AsyncSession, *, ctx: AuthContext, **values: object) -> SupervisoryVisit:
    agency_id = require_agency_admin(ctx)
    staff = (await session.execute(select(StaffProfile).where(StaffProfile.id == values["staff_id"], StaffProfile.agency_id == agency_id))).scalar_one_or_none()
    if staff is None:
        raise NotFoundError(message="Staff member not found.")
    if values.get("patient_id") is not None:
        await _patient(session, agency_id=agency_id, patient_id=values["patient_id"])
    item = SupervisoryVisit(agency_id=agency_id, supervisor_user_id=ctx.user_id, **values)
    session.add(item)
    await session.flush()
    return item


async def complete_supervisory_visit(session: AsyncSession, *, ctx: AuthContext, visit_id: uuid.UUID, findings: str) -> SupervisoryVisit:
    agency_id = require_agency_admin(ctx)
    item = (await session.execute(select(SupervisoryVisit).where(SupervisoryVisit.id == visit_id, SupervisoryVisit.agency_id == agency_id))).scalar_one_or_none()
    if item is None:
        raise NotFoundError(message="Supervisory visit not found.")
    item.status, item.findings, item.completed_at = "COMPLETED", findings, utc_now()
    await session.flush()
    return item


async def acknowledge_supervisory_visit(session: AsyncSession, *, ctx: AuthContext, visit_id: uuid.UUID) -> SupervisoryVisit:
    agency_id = agency_id_for(ctx)
    item = (await session.execute(select(SupervisoryVisit).where(SupervisoryVisit.id == visit_id, SupervisoryVisit.agency_id == agency_id))).scalar_one_or_none()
    if item is None:
        raise NotFoundError(message="Supervisory visit not found.")
    staff = (await session.execute(select(StaffProfile).where(StaffProfile.id == item.staff_id, StaffProfile.user_id == ctx.user_id))).scalar_one_or_none()
    if ctx.role is not UserRole.STAFF or staff is None:
        raise CrossAgencyAccessDeniedError(message="Only the supervised staff member may acknowledge this visit.")
    if item.status != "COMPLETED":
        raise ValidationError(message="Only a completed visit can be acknowledged.")
    item.acknowledged_at = utc_now()
    await session.flush()
    return item


async def list_reports(session: AsyncSession, *, ctx: AuthContext) -> list[AgencyComplianceReport]:
    agency_id = agency_id_for(ctx)
    stmt = select(AgencyComplianceReport).where(AgencyComplianceReport.agency_id == agency_id).order_by(AgencyComplianceReport.created_at.desc())
    if ctx.role is not UserRole.AGENCY_ADMIN:
        stmt = stmt.where(AgencyComplianceReport.reporter_user_id == ctx.user_id)
    return list((await session.execute(stmt)).scalars().all())


async def create_report(session: AsyncSession, *, ctx: AuthContext, **values: object) -> AgencyComplianceReport:
    if ctx.role not in {UserRole.AGENCY_ADMIN, UserRole.STAFF, UserRole.PATIENT, UserRole.GUARDIAN}:
        raise CrossAgencyAccessDeniedError(message="Your role cannot submit compliance reports.")
    await _validate_report_patient(session, ctx=ctx, patient_id=values.get("patient_id"))
    item = AgencyComplianceReport(agency_id=agency_id_for(ctx), reporter_user_id=ctx.user_id, category=values["category"].value, severity=values["severity"].value, title=values["title"], description=values["description"], patient_id=values.get("patient_id"))
    session.add(item)
    await session.flush()
    return item


async def resolve_report(session: AsyncSession, *, ctx: AuthContext, report_id: uuid.UUID, note: str, dismiss: bool) -> AgencyComplianceReport:
    agency_id = require_agency_admin(ctx)
    item = (await session.execute(select(AgencyComplianceReport).where(AgencyComplianceReport.id == report_id, AgencyComplianceReport.agency_id == agency_id))).scalar_one_or_none()
    if item is None:
        raise NotFoundError(message="Compliance report not found.")
    item.status = "DISMISSED" if dismiss else "RESOLVED"
    item.resolution_note, item.resolved_at, item.resolved_by_user_id = note, utc_now(), ctx.user_id
    await session.flush()
    return item


async def overview(session: AsyncSession, *, ctx: AuthContext) -> dict[str, int]:
    agency_id = agency_id_for(ctx)
    authorizations = await list_authorizations(session, ctx=ctx)
    visits = await list_supervisory_visits(session, ctx=ctx)
    reports = await list_reports(session, ctx=ctx)
    today = date.today()
    return {
        "authorizations_active": sum(i.status == "ACTIVE" for i in authorizations),
        "authorizations_expiring": sum(i.status == "EXPIRING" for i in authorizations),
        "authorizations_expired": sum(i.status == "EXPIRED" for i in authorizations),
        "supervisory_due": sum(i.status == "SCHEDULED" and i.scheduled_at.date() <= today + timedelta(days=30) for i in visits),
        "supervisory_completed_this_month": sum(i.status == "COMPLETED" and i.completed_at and i.completed_at.month == today.month and i.completed_at.year == today.year for i in visits),
        "reports_open": sum(i.status in {"OPEN", "IN_PROGRESS"} for i in reports),
        "reports_critical": sum(i.status in {"OPEN", "IN_PROGRESS"} and i.severity == "CRITICAL" for i in reports),
    }


async def names(session: AsyncSession, *, patient_id: uuid.UUID | None = None, staff_id: uuid.UUID | None = None, user_id: uuid.UUID | None = None) -> dict[str, str | None]:
    result: dict[str, str | None] = {"patient_name": None, "staff_name": None, "user_name": None}
    if patient_id:
        result["patient_name"] = await session.scalar(select(User.full_name).join(PatientProfile, PatientProfile.user_id == User.id).where(PatientProfile.id == patient_id))
    if staff_id:
        result["staff_name"] = await session.scalar(select(User.full_name).join(StaffProfile, StaffProfile.user_id == User.id).where(StaffProfile.id == staff_id))
    if user_id:
        result["user_name"] = await session.scalar(select(User.full_name).where(User.id == user_id))
    return result
