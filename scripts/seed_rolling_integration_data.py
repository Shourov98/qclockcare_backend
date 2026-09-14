"""Seed an agency's rolling, integration-ready schedule.

This seed is intentionally compact: it batches related inserts so it works
against remote development databases with higher latency. It replaces only
rows with its own marker and creates 48 appointments from today through the
following seven days, plus activities, current-day visits/EVV, and an inbox.

Run after ``seed_test_user.py`` or ``seed_agency_showcase.py``:

    uv run python scripts/seed_rolling_integration_data.py \
      --agency-admin-email admin@qlockcare.dev
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.core.config import settings

SEED_TAG = "[rolling-integration-seed:v1]"


async def seed(agency_admin_email: str) -> None:
    engine = create_async_engine(
        settings.effective_database_url,
        pool_pre_ping=True,
        connect_args={"statement_cache_size": 0},
    )
    try:
        async with engine.begin() as conn:
            agency_row = (await conn.execute(text("""
                SELECT a.id, a.name, u.id
                FROM users u
                JOIN user_roles ur ON ur.user_id = u.id
                JOIN agencies a ON a.id = ur.agency_id
                WHERE u.email = :email AND ur.role = 'AGENCY_ADMIN'
                LIMIT 1
            """), {"email": agency_admin_email})).first()
            if agency_row is None:
                raise SystemExit(f"No agency-admin account found for {agency_admin_email!r}.")
            agency_id, agency_name, admin_user_id = agency_row

            staff_rows = (await conn.execute(text("""
                SELECT id, user_id FROM staff_profiles
                WHERE agency_id = :agency_id AND status = 'ACTIVE'
                ORDER BY created_at, id
                LIMIT 12
            """), {"agency_id": agency_id})).all()
            patient_rows = (await conn.execute(text("""
                SELECT id FROM patient_profiles
                WHERE agency_id = :agency_id AND status = 'ACTIVE'
                ORDER BY created_at, id
                LIMIT 24
            """), {"agency_id": agency_id})).all()
            if not staff_rows or not patient_rows:
                raise SystemExit(
                    "The target agency needs active staff and patients. "
                    "Run seed_test_user.py or seed_agency_showcase.py first."
                )

            location_id = (await conn.execute(text("""
                SELECT id FROM locations
                WHERE agency_id = :agency_id AND deleted_at IS NULL AND is_active
                ORDER BY created_at, id
                LIMIT 1
            """), {"agency_id": agency_id})).scalar_one_or_none()
            if location_id is None:
                location_id = uuid.uuid4()
                await conn.execute(text("""
                    INSERT INTO locations (
                        id, agency_id, label, address_line1, city, state,
                        postal_code, country, latitude, longitude,
                        geofence_radius_m, is_active
                    ) VALUES (
                        :id, :agency_id, 'Integration Test Residence', '123 Oak St',
                        'Springfield', 'MN', '55101', 'US', 44.977800, -93.265000,
                        150, true
                    )
                """), {"id": location_id, "agency_id": agency_id})

            await conn.execute(text("""
                DELETE FROM appointment_activities aa
                USING appointments a
                WHERE aa.appointment_id = a.id
                  AND a.agency_id = :agency_id
                  AND a.notes LIKE :tag
            """), {"agency_id": agency_id, "tag": f"{SEED_TAG}%"})
            await conn.execute(text("""
                DELETE FROM appointments
                WHERE agency_id = :agency_id AND notes LIKE :tag
            """), {"agency_id": agency_id, "tag": f"{SEED_TAG}%"})
            await conn.execute(text("""
                DELETE FROM notifications
                WHERE agency_id = :agency_id AND metadata->>'source' = 'rolling_integration_seed'
            """), {"agency_id": agency_id})

            now = datetime.now(tz=UTC)
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            # The first three slots are intentionally early so the seed has
            # completed, awaiting-signature, and missed examples on any
            # normal daytime run, not only future appointments.
            slots = (1, 3, 5, 11, 14, 17)
            service_cycle = (
                ("Morning personal care", "PCA", 4500),
                ("Medication and wellness check", "PCA", 4500),
                ("Community skills support", "245D", 6750),
                ("Care plan check-in", "CFSS", 4500),
            )
            appointments: list[dict[str, object]] = []
            activities: list[dict[str, object]] = []
            visits: list[dict[str, object]] = []
            evv_records: list[dict[str, object]] = []
            deliveries: list[dict[str, object]] = []

            for index in range(48):
                day_offset, hour = divmod(index, len(slots))
                scheduled_start = day_start + timedelta(days=day_offset, hours=slots[hour])
                service, program_type, amount = service_cycle[index % len(service_cycle)]
                status = "SCHEDULED"
                cancelled_at = None
                cancelled_reason = None
                with_visit = False
                if day_offset == 0 and scheduled_start < now:
                    status = ("COMPLETED", "AWAITING_SIGNATURE", "MISSED", "CANCELLED")[index % 4]
                    with_visit = status in {"COMPLETED", "AWAITING_SIGNATURE"}
                    if status == "MISSED":
                        cancelled_at = scheduled_start + timedelta(minutes=20)
                        cancelled_reason = "Caregiver was unavailable; the family was notified."
                    elif status == "CANCELLED":
                        cancelled_at = scheduled_start - timedelta(hours=1)
                        cancelled_reason = "Family requested a reschedule."
                elif day_offset == 0 and index % 3 == 0:
                    status = "READY"
                elif day_offset in {2, 5} and index % 13 == 0:
                    status = "CANCELLED"
                    cancelled_at = now
                    cancelled_reason = "Family cancelled the upcoming visit."

                appointment_id = uuid.uuid4()
                staff_id, staff_user_id = staff_rows[index % len(staff_rows)]
                if status == "SCHEDULED" and index % 9 == 0:
                    staff_id = None
                billing_status = (
                    "paid" if status == "COMPLETED"
                    else "cancelled" if status in {"CANCELLED", "MISSED"}
                    else "pending"
                )
                appointments.append({
                    "id": appointment_id,
                    "agency_id": agency_id,
                    "patient_id": patient_rows[index % len(patient_rows)][0],
                    "staff_id": staff_id,
                    "program_type": program_type,
                    "scheduled_start": scheduled_start,
                    "scheduled_end": scheduled_start + timedelta(hours=1),
                    "status": status,
                    "location": "Integration Test Residence - 123 Oak St, Springfield",
                    "location_id": location_id,
                    "notes": f"{SEED_TAG} {service}. Related test appointment.",
                    "cancelled_at": cancelled_at,
                    "cancelled_reason": cancelled_reason,
                    "billing_status": billing_status,
                    "billing_amount_cents": amount,
                    "billing_paid_at": scheduled_start + timedelta(hours=1, minutes=5)
                    if billing_status == "paid" else None,
                    "billing_paid_by_user_id": staff_user_id if billing_status == "paid" else None,
                    "claim_id": f"CG-TEST-{str(appointment_id)[:8].upper()}",
                })
                activity_ids = [uuid.uuid4(), uuid.uuid4()]
                for activity_id, name, minutes in (
                    (activity_ids[0], service, 35),
                    (activity_ids[1], "Document care outcomes", 15),
                ):
                    activities.append({
                        "id": activity_id, "appointment_id": appointment_id,
                        "agency_id": agency_id, "name": name, "planned_minutes": minutes,
                        "notes": "Integration-test checklist item.",
                    })
                if with_visit and staff_id is not None:
                    visit_id = uuid.uuid4()
                    visit_status = "COMPLETED" if status == "COMPLETED" else "AWAITING_SIGNATURE"
                    visits.append({
                        "id": visit_id, "appointment_id": appointment_id,
                        "agency_id": agency_id, "staff_id": staff_id, "status": visit_status,
                        "billing_confirmed_at": scheduled_start + timedelta(hours=1)
                        if visit_status == "AWAITING_SIGNATURE" else None,
                    })
                    evv_records.append({
                        "id": uuid.uuid4(), "visit_id": visit_id, "agency_id": agency_id,
                        "start_time": scheduled_start,
                        "end_time": scheduled_start + timedelta(hours=1),
                    })
                    deliveries.extend({
                        "id": uuid.uuid4(), "visit_id": visit_id, "agency_id": agency_id,
                        "activity_id": activity_id,
                        "completed_at": scheduled_start + timedelta(hours=1),
                    } for activity_id in activity_ids)

            await conn.execute(text("""
                INSERT INTO appointments (
                    id, agency_id, patient_id, staff_id, program_type, scheduled_start,
                    scheduled_end, status, location, location_id, notes, cancelled_at,
                    cancelled_reason, billing_status, billing_amount_cents, billing_paid_at,
                    billing_paid_by_user_id, claim_id
                ) VALUES (
                    :id, :agency_id, :patient_id, :staff_id, :program_type, :scheduled_start,
                    :scheduled_end, :status, :location, :location_id, :notes, :cancelled_at,
                    :cancelled_reason, :billing_status, :billing_amount_cents, :billing_paid_at,
                    :billing_paid_by_user_id, :claim_id
                )
            """), appointments)
            await conn.execute(text("""
                INSERT INTO appointment_activities (
                    id, appointment_id, agency_id, name, planned_minutes, status, notes
                ) VALUES (
                    :id, :appointment_id, :agency_id, :name, :planned_minutes,
                    'PENDING', :notes
                )
            """), activities)
            if visits:
                await conn.execute(text("""
                    INSERT INTO visits (
                        id, appointment_id, agency_id, staff_id, status, sharing_location,
                        billing_confirmed_at
                    ) VALUES (
                        :id, :appointment_id, :agency_id, :staff_id, :status, false,
                        :billing_confirmed_at
                    )
                """), visits)
                await conn.execute(text("""
                    INSERT INTO evv_records (
                        id, visit_id, agency_id, start_time, start_lat, start_lng,
                        start_accuracy_m, start_device_id, start_verification_status,
                        end_time, end_lat, end_lng, end_accuracy_m
                    ) VALUES (
                        :id, :visit_id, :agency_id, :start_time, 44.977800, -93.265000,
                        10.0, 'integration-seed-device', 'VERIFIED',
                        :end_time, 44.977800, -93.265000, 12.0
                    )
                """), evv_records)
                await conn.execute(text("""
                    INSERT INTO visit_activity_deliveries (
                        id, visit_id, agency_id, activity_id, status, completed_at
                    ) VALUES (
                        :id, :visit_id, :agency_id, :activity_id, 'DONE', :completed_at
                    )
                """), deliveries)

            notification_specs = (
                ("APPOINTMENT_READY", "Visit ready for review", "A today visit is ready for caregiver review."),
                ("APPOINTMENT_ASSIGNED", "Caregiver assigned", "An upcoming visit now has an assigned caregiver."),
                ("VISIT_SUBMITTED_FOR_SIGNATURE", "Signature requested", "A completed visit awaits signature."),
                ("BILLING_CONFIRMED", "Payment received", "A completed appointment has been marked paid."),
                ("APPOINTMENT_CANCELLED", "Visit cancelled", "A future visit was cancelled by the family."),
                ("GENERIC", "Coverage review", "One upcoming appointment remains open for assignment."),
            )
            notifications = [{
                "id": uuid.uuid4(), "agency_id": agency_id, "recipient_user_id": admin_user_id,
                "type": kind, "title": title, "body": body,
                "status": "READ" if index in {0, 3} else "SENT",
                "metadata": json.dumps({"source": "rolling_integration_seed"}),
                "created_at": now - timedelta(minutes=index * 15),
                "read_at": now - timedelta(minutes=index * 15) if index in {0, 3} else None,
            } for index, (kind, title, body) in enumerate(notification_specs)]
            await conn.execute(text("""
                INSERT INTO notifications (
                    id, agency_id, recipient_user_id, type, title, body, status,
                    metadata, created_at, read_at
                ) VALUES (
                    :id, :agency_id, :recipient_user_id, :type, :title, :body, :status,
                    CAST(:metadata AS jsonb), :created_at, :read_at
                )
            """), notifications)

        print(
            f"Seeded {agency_name}: {len(appointments)} appointments, {len(activities)} activities, "
            f"{len(visits)} visits, {len(evv_records)} EVV records, and {len(notifications)} notifications."
        )
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed rolling integration data for one agency.")
    parser.add_argument("--agency-admin-email", default="admin@qlockcare.dev")
    args = parser.parse_args()
    asyncio.run(seed(args.agency_admin_email))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
