from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import String, case, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.appointments.models import Appointment
from src.modules.compliance.models import AgencyComplianceReport, ServiceAuthorization
from src.modules.group_homes.models import GroupHome
from src.modules.identity.models import User
from src.modules.patients.models import PatientProfile
from src.modules.staff.models import StaffProfile
from src.modules.visits.models import AppointmentSignature, EVVRecord, Visit
from src.shared.domain.enums import AppointmentStatus, UserStatus, VisitStatus


def _month_start(value: datetime) -> datetime:
    return datetime(value.year, value.month, 1, tzinfo=UTC)


async def overview(session: AsyncSession, *, agency_id: UUID, months: int = 6) -> dict:
    now = datetime.now(UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + timedelta(days=1)

    active_clients = (await session.scalar(select(func.count(PatientProfile.id)).where(
        PatientProfile.agency_id == agency_id,
        PatientProfile.deleted_at.is_(None),
        PatientProfile.status == UserStatus.ACTIVE,
    ))) or 0
    active_staff = (await session.scalar(select(func.count(StaffProfile.id)).where(
        StaffProfile.agency_id == agency_id,
        StaffProfile.deleted_at.is_(None),
        StaffProfile.status == UserStatus.ACTIVE,
    ))) or 0
    visits_today = (await session.scalar(select(func.count(Appointment.id)).where(
        Appointment.agency_id == agency_id,
        Appointment.scheduled_start >= today_start,
        Appointment.scheduled_start < tomorrow_start,
        Appointment.status.notin_([AppointmentStatus.CANCELLED, AppointmentStatus.MISSED, AppointmentStatus.REJECTED]),
    ))) or 0
    sa_units_used, sa_units_authorized = (await session.execute(select(
        func.coalesce(func.sum(ServiceAuthorization.used_units), 0),
        func.coalesce(func.sum(ServiceAuthorization.authorized_units), 0),
    ).where(ServiceAuthorization.agency_id == agency_id, ServiceAuthorization.deleted_at.is_(None)))).one()

    start_month = _month_start(now)
    for _ in range(months - 1):
        previous = start_month.month - 1 or 12
        year = start_month.year - (1 if start_month.month == 1 else 0)
        start_month = start_month.replace(year=year, month=previous)

    month_expr = func.date_trunc("month", Appointment.scheduled_start)
    appointment_months = (await session.execute(select(
        month_expr.label("month"),
        func.coalesce(func.sum(case((Appointment.billing_status == "paid", Appointment.billing_amount_cents), else_=0)), 0).label("paid"),
        func.count(Appointment.id).filter(Appointment.status == AppointmentStatus.COMPLETED).label("completed"),
    ).where(Appointment.agency_id == agency_id, Appointment.scheduled_start >= start_month).group_by(month_expr))).all()
    staff_months = (await session.execute(select(
        func.date_trunc("month", StaffProfile.created_at).label("month"), func.count(StaffProfile.id),
    ).where(StaffProfile.agency_id == agency_id, StaffProfile.created_at >= start_month).group_by(func.date_trunc("month", StaffProfile.created_at)))).all()
    patient_months = (await session.execute(select(
        func.date_trunc("month", PatientProfile.created_at).label("month"), func.count(PatientProfile.id),
    ).where(PatientProfile.agency_id == agency_id, PatientProfile.created_at >= start_month).group_by(func.date_trunc("month", PatientProfile.created_at)))).all()

    by_month: dict[datetime, dict[str, int]] = {}
    for month, paid, completed in appointment_months:
        by_month.setdefault(_month_start(month), {}).update(paid_amount_cents=int(paid), completed_visits=int(completed))
    for month, count in staff_months:
        by_month.setdefault(_month_start(month), {})["new_staff"] = int(count)
    for month, count in patient_months:
        by_month.setdefault(_month_start(month), {})["new_clients"] = int(count)
    trends = []
    cursor = start_month
    for _ in range(months):
        values = by_month.get(cursor, {})
        trends.append({"month": cursor.date(), "paid_amount_cents": values.get("paid_amount_cents", 0), "completed_visits": values.get("completed_visits", 0), "new_staff": values.get("new_staff", 0), "new_clients": values.get("new_clients", 0)})
        next_month = cursor.month % 12 + 1
        cursor = cursor.replace(year=cursor.year + (1 if cursor.month == 12 else 0), month=next_month)

    completed_visits = (await session.scalar(select(func.count(Visit.id)).where(Visit.agency_id == agency_id, Visit.status == VisitStatus.COMPLETED))) or 0
    evv_captured = (await session.scalar(select(func.count(Visit.id)).join(EVVRecord, EVVRecord.visit_id == Visit.id).where(Visit.agency_id == agency_id, Visit.status == VisitStatus.COMPLETED, EVVRecord.start_time.is_not(None)))) or 0
    signed = (await session.scalar(select(func.count(Visit.id)).join(AppointmentSignature, AppointmentSignature.visit_id == Visit.id).where(Visit.agency_id == agency_id, Visit.status == VisitStatus.COMPLETED))) or 0
    authorization_total = (await session.scalar(select(func.count(ServiceAuthorization.id)).where(ServiceAuthorization.agency_id == agency_id, ServiceAuthorization.deleted_at.is_(None)))) or 0
    authorization_ok = (await session.scalar(select(func.count(ServiceAuthorization.id)).where(ServiceAuthorization.agency_id == agency_id, ServiceAuthorization.deleted_at.is_(None), ServiceAuthorization.status.in_(["ACTIVE", "EXPIRING"])))) or 0
    open_reports = (await session.scalar(select(func.count(AgencyComplianceReport.id)).where(AgencyComplianceReport.agency_id == agency_id, AgencyComplianceReport.status.in_(["OPEN", "IN_PROGRESS"])))) or 0
    expired_authorizations = (await session.scalar(select(func.count(ServiceAuthorization.id)).where(ServiceAuthorization.agency_id == agency_id, ServiceAuthorization.deleted_at.is_(None), ServiceAuthorization.status.in_(["EXPIRED", "EXHAUSTED"])))) or 0
    evv_score = 100 if not completed_visits else round(100 * evv_captured / completed_visits)
    documentation_score = 100 if not completed_visits else round(100 * signed / completed_visits)
    authorization_score = 100 if not authorization_total else round(100 * authorization_ok / authorization_total)
    audit_readiness = {"score": round((evv_score + documentation_score + authorization_score) / 3), "evv_score": evv_score, "documentation_score": documentation_score, "authorization_score": authorization_score, "open_reports": int(open_reports), "expired_authorizations": int(expired_authorizations)}
    return {"active_clients": int(active_clients), "active_staff": int(active_staff), "visits_today": int(visits_today), "sa_units_used": float(sa_units_used), "sa_units_authorized": float(sa_units_authorized), "recent_visits_count": int(completed_visits), "trends": trends, "audit_readiness": audit_readiness}


async def search(session: AsyncSession, *, agency_id: UUID, query: str, limit: int = 20) -> list[dict]:
    pattern = f"%{query.strip()}%"
    if not query.strip():
        return []
    patient_rows = (await session.execute(select(PatientProfile.id, User.full_name, PatientProfile.patient_code).join(User, User.id == PatientProfile.user_id).where(PatientProfile.agency_id == agency_id, PatientProfile.deleted_at.is_(None), or_(User.full_name.ilike(pattern), PatientProfile.patient_code.ilike(pattern))).limit(limit))).all()
    staff_rows = (await session.execute(select(StaffProfile.id, User.full_name, StaffProfile.staff_code).join(User, User.id == StaffProfile.user_id).where(StaffProfile.agency_id == agency_id, StaffProfile.deleted_at.is_(None), or_(User.full_name.ilike(pattern), StaffProfile.staff_code.ilike(pattern))).limit(limit))).all()
    appointment_rows = (await session.execute(select(Appointment.id, Appointment.claim_id, Appointment.status, Appointment.scheduled_start).where(Appointment.agency_id == agency_id, or_(Appointment.claim_id.ilike(pattern), cast(Appointment.id, String).ilike(pattern))).limit(limit))).all()
    home_rows = (await session.execute(select(GroupHome.id, GroupHome.name).where(GroupHome.agency_id == agency_id, GroupHome.name.ilike(pattern)).limit(limit))).all()
    results = ([{"kind": "patient", "id": row.id, "title": row.full_name or row.patient_code, "subtitle": row.patient_code, "href": f"/clients?patient={row.id}"} for row in patient_rows] + [{"kind": "staff", "id": row.id, "title": row.full_name or row.staff_code, "subtitle": row.staff_code, "href": f"/staff?staff={row.id}"} for row in staff_rows] + [{"kind": "appointment", "id": row.id, "title": row.claim_id or f"Appointment {str(row.id)[:8]}", "subtitle": f"{row.status.value} · {row.scheduled_start.isoformat()}", "href": f"/scheduling/{row.id}"} for row in appointment_rows] + [{"kind": "group_home", "id": row.id, "title": row.name, "subtitle": "Group home", "href": f"/group-homes/{row.id}"} for row in home_rows])
    return results[:limit]
