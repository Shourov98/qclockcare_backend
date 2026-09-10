"""Seed related agency compliance demonstration data.

This is safe to rerun. It replaces only records carrying its seed marker and
uses staff, patients, and guardians that already belong to the selected agency.

    uv run python scripts/seed_compliance_showcase.py \
      --agency-admin-email agencyadmin01@qlockcare.dev
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.core.config import settings

TAG = "[compliance-showcase:v1]"


async def seed(admin_email: str) -> None:
    engine = create_async_engine(
        settings.effective_database_url,
        pool_pre_ping=True,
        connect_args={"statement_cache_size": 0},
    )
    today = date.today()
    now = datetime.now(tz=UTC)
    try:
        async with engine.begin() as conn:
            agency = (await conn.execute(text("""
                SELECT ur.agency_id, u.id FROM users u JOIN user_roles ur ON ur.user_id = u.id
                WHERE u.email = :email AND ur.role = 'AGENCY_ADMIN' LIMIT 1
            """), {"email": admin_email})).first()
            if agency is None:
                raise SystemExit(f"{admin_email!r} is not an agency administrator.")
            agency_id, admin_user_id = agency
            staff = (await conn.execute(text("""
                SELECT sp.id, sp.user_id FROM staff_profiles sp
                WHERE sp.agency_id = :agency AND sp.status = 'ACTIVE' ORDER BY sp.created_at LIMIT 4
            """), {"agency": agency_id})).all()
            patients = (await conn.execute(text("""
                SELECT pp.id, pp.user_id FROM patient_profiles pp
                WHERE pp.agency_id = :agency AND pp.deleted_at IS NULL AND pp.status = 'ACTIVE'
                ORDER BY pp.created_at LIMIT 5
            """), {"agency": agency_id})).all()
            guardians = (await conn.execute(text("""
                SELECT gp.id, gp.user_id FROM guardian_profiles gp
                WHERE gp.agency_id = :agency AND gp.deleted_at IS NULL AND gp.status = 'ACTIVE'
                ORDER BY gp.created_at LIMIT 2
            """), {"agency": agency_id})).all()
            if len(staff) < 2 or len(patients) < 3 or not guardians:
                raise SystemExit("The agency needs at least 2 active staff, 3 patients, and 1 guardian. Run seed_agency_showcase.py first.")

            await conn.execute(text("DELETE FROM agency_compliance_reports WHERE agency_id = :agency AND description LIKE :tag"), {"agency": agency_id, "tag": f"%{TAG}%"})
            await conn.execute(text("DELETE FROM supervisory_visits WHERE agency_id = :agency AND (objectives LIKE :tag OR findings LIKE :tag)"), {"agency": agency_id, "tag": f"%{TAG}%"})
            await conn.execute(text("DELETE FROM service_authorizations WHERE agency_id = :agency AND notes LIKE :tag"), {"agency": agency_id, "tag": f"%{TAG}%"})

            authorization_rows = [
                (patients[0][0], "PCA", "Personal care assistance", "AUTH-PCA-2026-041", today - timedelta(days=60), today + timedelta(days=150), 160, 48, "hours", "ACTIVE", "Routine personal care authorization."),
                (patients[1][0], "CFSS", "Homemaker services", "AUTH-CFSS-2026-118", today - timedelta(days=180), today + timedelta(days=12), 90, 78, "hours", "EXPIRING", "Renewal paperwork has been requested."),
                (patients[2][0], "245D", "Community support", "AUTH-245D-2025-009", today - timedelta(days=400), today - timedelta(days=8), 120, 120, "hours", "EXPIRED", "Awaiting renewed county authorization."),
                (patients[3][0], "PCA", "Respite care", "AUTH-PCA-2026-072", today - timedelta(days=120), today + timedelta(days=80), 40, 40, "hours", "EXHAUSTED", "All approved respite units have been used."),
                (patients[4][0], "PCA", "Personal care assistance", "AUTH-PCA-2026-091", today - timedelta(days=20), today + timedelta(days=260), 200, 15, "hours", "ACTIVE", "New authorization for ongoing support."),
            ]
            for patient_id, program, service, number, starts, ends, authorized, used, unit, status, note in authorization_rows:
                await conn.execute(text("""
                    INSERT INTO service_authorizations (id, agency_id, patient_id, program_type, service_name, authorization_number, starts_on, ends_on, authorized_units, used_units, unit_label, status, notes)
                    VALUES (:id, :agency, :patient, :program, :service, :number, :starts, :ends, :authorized, :used, :unit, :status, :notes)
                """), {"id": uuid.uuid4(), "agency": agency_id, "patient": patient_id, "program": program, "service": service, "number": number, "starts": starts, "ends": ends, "authorized": authorized, "used": used, "unit": unit, "status": status, "notes": f"{TAG} {note}"})

            visit_rows = [
                (staff[0][0], patients[0][0], now - timedelta(days=32), now - timedelta(days=32), "COMPLETED", "Observe care-plan delivery and documentation.", "Observed safe transfer technique and complete documentation."),
                (staff[1][0], patients[1][0], now - timedelta(days=7), now - timedelta(days=7), "COMPLETED", "Review CFSS task delivery.", "Documentation improvement coaching completed."),
                (staff[2][0], patients[2][0], now + timedelta(days=4), None, "SCHEDULED", "Review community support plan and service notes.", None),
                (staff[3][0], None, now + timedelta(days=14), None, "SCHEDULED", "Quarterly general supervisory check-in.", None),
            ]
            for staff_id, patient_id, scheduled, completed, status, objectives, findings in visit_rows:
                await conn.execute(text("""
                    INSERT INTO supervisory_visits (id, agency_id, staff_id, patient_id, supervisor_user_id, scheduled_at, completed_at, status, objectives, findings, acknowledged_at)
                    VALUES (:id, :agency, :staff, :patient, :supervisor, :scheduled, :completed, :status, :objectives, :findings, :acknowledged)
                """), {"id": uuid.uuid4(), "agency": agency_id, "staff": staff_id, "patient": patient_id, "supervisor": admin_user_id, "scheduled": scheduled, "completed": completed, "status": status, "objectives": f"{TAG} {objectives}", "findings": f"{TAG} {findings}" if findings else None, "acknowledged": completed if completed else None})

            report_rows = [
                (staff[0][1], patients[0][0], "STAFF_CREDENTIAL", "HIGH", "Credential renewal needs review", "CPR renewal is due this month and the staff file requires confirmation."),
                (patients[1][1], patients[1][0], "SERVICE_AUTH", "MEDIUM", "Authorization balance question", "I would like the agency to confirm remaining approved service hours."),
                (guardians[0][1], patients[0][0], "DOCUMENTATION", "LOW", "Care-plan documentation follow-up", "Please confirm the most recent care-plan update is on file."),
                (staff[2][1], patients[2][0], "SAFETY", "CRITICAL", "Home safety concern", "A safety concern was reported and needs agency review before the next visit."),
                (guardians[0][1], patients[1][0], "OTHER", "LOW", "Resolved communication follow-up", "The agency has already addressed this demonstration concern."),
            ]
            for index, (reporter, patient, category, severity, title, description) in enumerate(report_rows):
                resolved = index == len(report_rows) - 1
                await conn.execute(text("""
                    INSERT INTO agency_compliance_reports (id, agency_id, reporter_user_id, patient_id, category, severity, status, title, description, resolution_note, resolved_at, resolved_by_user_id)
                    VALUES (:id, :agency, :reporter, :patient, :category, :severity, :status, :title, :description, :note, :resolved_at, :resolved_by)
                """), {"id": uuid.uuid4(), "agency": agency_id, "reporter": reporter, "patient": patient, "category": category, "severity": severity, "status": "RESOLVED" if resolved else "OPEN", "title": title, "description": f"{TAG} {description}", "note": "Reviewed and resolved during agency follow-up." if resolved else None, "resolved_at": now - timedelta(days=1) if resolved else None, "resolved_by": admin_user_id if resolved else None})
        print("Seeded 5 authorizations, 4 supervisory visits, and 5 compliance reports.")
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agency-admin-email", required=True)
    args = parser.parse_args()
    asyncio.run(seed(args.agency_admin_email))


if __name__ == "__main__":
    main()
