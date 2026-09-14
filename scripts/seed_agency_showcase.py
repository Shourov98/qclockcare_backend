"""Create a substantial, agency-scoped demonstration dataset.

The normal lifecycle seed intentionally stays small.  This script is for a
developer or client who needs enough *related* records to exercise the agency
dashboard: roster pages, client pages, locations, group homes, schedules,
billing, and notifications.

It is safe to re-run.  It only creates deterministic ``showcase`` accounts in
the selected agency and replaces only appointments that carry its seed marker.
Its calendar always covers today through the following seven days.
It never deletes another agency's records.

Example:

    uv run python scripts/seed_agency_showcase.py \
      --agency-admin-email agencyadmin01@qlockcare.dev

All generated accounts use ``ShowcasePass123!``.  Their emails are printed at
the end of the run and include the target agency id, so they cannot collide
with accounts in another tenant.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from src.core.config import settings
from src.core.security import hash_password

PASSWORD = "ShowcasePass123!"
SEED_TAG = "[showcase-seed:v1]"

STAFF_NAMES = [
    "Amelia Brooks", "Noah Campbell", "Olivia Carter", "Ethan Davis",
    "Sophia Evans", "Liam Foster", "Mia Garcia", "Lucas Hall",
    "Ava Irving", "Mason Jones", "Isabella King", "Henry Lewis",
]
PATIENT_NAMES = [
    "Evelyn Adams", "Benjamin Bell", "Charlotte Cole", "Daniel Cruz",
    "Grace Diaz", "Samuel Ellis", "Harper Flores", "Jack Green",
    "Ella Harris", "Owen Ingram", "Lily Jackson", "Caleb Knight",
    "Nora Lane", "Wyatt Moore", "Zoe Nelson", "Isaac Ortiz",
    "Ruby Price", "Leo Quinn", "Scarlett Reed", "Julian Scott",
    "Violet Turner", "Aiden Underwood", "Hannah Valdez", "Mateo White",
]
GUARDIAN_NAMES = [
    "Patricia Adams", "Robert Bell", "Monica Cole", "Thomas Cruz",
    "Janet Diaz", "Edward Ellis",
]
LOCATIONS = [
    ("Maple House", "415 Maple Ave", "Saint Paul", "MN", "55103", 44.9537, -93.0900),
    ("Harbor House", "810 Harbor View", "Minneapolis", "MN", "55401", 44.9854, -93.2705),
    ("Cedar Residence", "72 Cedar Lane", "Saint Paul", "MN", "55106", 44.9634, -93.0540),
    ("Northside Day Program", "2200 North 4th St", "Minneapolis", "MN", "55411", 45.0031, -93.2882),
]


@dataclass(frozen=True)
class Agency:
    id: uuid.UUID
    name: str


async def _resolve_agency(engine: AsyncEngine, email: str) -> Agency:
    async with engine.connect() as conn:
        row = (await conn.execute(text("""
            SELECT a.id, a.name
            FROM users u
            JOIN user_roles ur ON ur.user_id = u.id
            JOIN agencies a ON a.id = ur.agency_id
            WHERE u.email = :email AND ur.role = 'AGENCY_ADMIN'
            LIMIT 1
        """), {"email": email})).first()
    if row is None:
        raise SystemExit(f"ERROR: {email!r} is not an agency administrator.")
    return Agency(id=row[0], name=row[1])


async def _ensure_user(
    engine: AsyncEngine, *, agency_id: uuid.UUID, email: str, full_name: str, role: str
) -> uuid.UUID:
    """Return an account id and guarantee its agency role exists."""
    async with engine.begin() as conn:
        user_id = (await conn.execute(
            text("SELECT id FROM users WHERE email = :email"), {"email": email}
        )).scalar_one_or_none()
        if user_id is None:
            user_id = uuid.uuid4()
            await conn.execute(text("""
                INSERT INTO users (
                    id, email, password_hash, full_name, status, email_verified_at,
                    must_change_password, failed_login_attempts
                ) VALUES (:id, :email, :password_hash, :full_name, 'ACTIVE', now(), false, 0)
            """), {
                "id": user_id, "email": email, "password_hash": hash_password(PASSWORD),
                "full_name": full_name,
            })
        has_role = (await conn.execute(text("""
            SELECT 1 FROM user_roles
            WHERE user_id = :user_id AND agency_id = :agency_id AND role = :role
        """), {"user_id": user_id, "agency_id": agency_id, "role": role})).scalar_one_or_none()
        if has_role is None:
            await conn.execute(text("""
                INSERT INTO user_roles (id, user_id, agency_id, role)
                VALUES (:id, :user_id, :agency_id, :role)
            """), {"id": uuid.uuid4(), "user_id": user_id, "agency_id": agency_id, "role": role})
    return user_id


async def _ensure_roster(engine: AsyncEngine, agency: Agency) -> tuple[list[uuid.UUID], list[uuid.UUID], list[uuid.UUID]]:
    key = str(agency.id).split("-")[0]
    staff_ids: list[uuid.UUID] = []
    patient_ids: list[uuid.UUID] = []
    guardian_ids: list[uuid.UUID] = []

    for index, name in enumerate(STAFF_NAMES, start=1):
        email = f"showcase.{key}.staff{index:02d}@qlockcare.dev"
        user_id = await _ensure_user(engine, agency_id=agency.id, email=email, full_name=name, role="STAFF")
        async with engine.begin() as conn:
            profile_id = (await conn.execute(text("""
                SELECT id FROM staff_profiles WHERE agency_id = :agency_id AND user_id = :user_id
            """), {"agency_id": agency.id, "user_id": user_id})).scalar_one_or_none()
            if profile_id is None:
                profile_id = uuid.uuid4()
                await conn.execute(text("""
                INSERT INTO staff_profiles (id, agency_id, user_id, staff_code, status, hired_at)
                    VALUES (:id, :agency_id, :user_id, :staff_code, 'ACTIVE', :hired_at)
                """), {"id": profile_id, "agency_id": agency.id, "user_id": user_id,
                       "staff_code": f"SC-{index:03d}",
                       "hired_at": date.today() - timedelta(days=30 + index * 17)})
            staff_ids.append(profile_id)

    for index, name in enumerate(PATIENT_NAMES, start=1):
        email = f"showcase.{key}.patient{index:02d}@qlockcare.dev"
        user_id = await _ensure_user(engine, agency_id=agency.id, email=email, full_name=name, role="PATIENT")
        async with engine.begin() as conn:
            profile_id = (await conn.execute(text("""
                SELECT id FROM patient_profiles WHERE agency_id = :agency_id AND user_id = :user_id
            """), {"agency_id": agency.id, "user_id": user_id})).scalar_one_or_none()
            if profile_id is None:
                profile_id = uuid.uuid4()
                await conn.execute(text("""
                    INSERT INTO patient_profiles (
                        id, agency_id, user_id, patient_code, status, date_of_birth,
                        preferred_language, care_notes, admitted_at
                    ) VALUES (
                        :id, :agency_id, :user_id, :patient_code, 'ACTIVE',
                        :date_of_birth, 'English', :care_notes, :admitted_at
                    )
                """), {"id": profile_id, "agency_id": agency.id, "user_id": user_id,
                       "patient_code": f"SC-P-{index:03d}",
                       "date_of_birth": date(date.today().year - (56 + index % 30), 6, 15),
                       "care_notes": "Showcase care plan: routine personal care and wellness monitoring.",
                       "admitted_at": date.today() - timedelta(days=30 + index * 9)})
            patient_ids.append(profile_id)

    for index, name in enumerate(GUARDIAN_NAMES, start=1):
        email = f"showcase.{key}.guardian{index:02d}@qlockcare.dev"
        user_id = await _ensure_user(engine, agency_id=agency.id, email=email, full_name=name, role="GUARDIAN")
        async with engine.begin() as conn:
            profile_id = (await conn.execute(text("""
                SELECT id FROM guardian_profiles WHERE agency_id = :agency_id AND user_id = :user_id
            """), {"agency_id": agency.id, "user_id": user_id})).scalar_one_or_none()
            if profile_id is None:
                profile_id = uuid.uuid4()
                await conn.execute(text("""
                    INSERT INTO guardian_profiles (
                        id, agency_id, user_id, status, contact_phone, contact_email, notes
                    ) VALUES (:id, :agency_id, :user_id, 'ACTIVE', :phone, :email, :notes)
                """), {"id": profile_id, "agency_id": agency.id, "user_id": user_id,
                       "phone": f"+1-651-555-{1200 + index}", "email": email,
                       "notes": "Showcase legal guardian and emergency contact."})
            guardian_ids.append(profile_id)

    async with engine.begin() as conn:
        for guardian_index, guardian_id in enumerate(guardian_ids):
            for patient_id in patient_ids[guardian_index * 4:(guardian_index + 1) * 4]:
                existing = (await conn.execute(text("""
                    SELECT 1 FROM patient_guardian_relationships
                    WHERE agency_id = :agency_id AND patient_id = :patient_id
                      AND guardian_id = :guardian_id AND relationship_type = 'GUARDIAN'
                """), {"agency_id": agency.id, "patient_id": patient_id, "guardian_id": guardian_id})).scalar_one_or_none()
                if existing is None:
                    await conn.execute(text("""
                        INSERT INTO patient_guardian_relationships (
                            id, agency_id, patient_id, guardian_id, relationship_type,
                            is_legal, valid_from
                        ) VALUES (:id, :agency_id, :patient_id, :guardian_id, 'GUARDIAN', true, current_date - 90)
                    """), {"id": uuid.uuid4(), "agency_id": agency.id, "patient_id": patient_id,
                           "guardian_id": guardian_id})
    return staff_ids, patient_ids, guardian_ids


async def _ensure_locations(engine: AsyncEngine, agency: Agency) -> list[uuid.UUID]:
    ids: list[uuid.UUID] = []
    for label, line1, city, state, postal, lat, lng in LOCATIONS:
        async with engine.begin() as conn:
            location_id = (await conn.execute(text("""
                SELECT id FROM locations
                WHERE agency_id = :agency_id AND label = :label AND deleted_at IS NULL
                LIMIT 1
            """), {"agency_id": agency.id, "label": label})).scalar_one_or_none()
            if location_id is None:
                location_id = uuid.uuid4()
                await conn.execute(text("""
                    INSERT INTO locations (
                        id, agency_id, label, address_line1, city, state, postal_code,
                        country, latitude, longitude, geofence_radius_m, is_active
                    ) VALUES (
                        :id, :agency_id, :label, :line1, :city, :state, :postal,
                        'US', :lat, :lng, 150, true
                    )
                """), {"id": location_id, "agency_id": agency.id, "label": label,
                       "line1": line1, "city": city, "state": state, "postal": postal,
                       "lat": lat, "lng": lng})
            ids.append(location_id)
    return ids


async def _ensure_group_homes(
    engine: AsyncEngine, agency: Agency, *, locations: list[uuid.UUID], patients: list[uuid.UUID],
    guardians: list[uuid.UUID], staff: list[uuid.UUID]
) -> int:
    definitions = [
        ("Maple House", locations[0], patients[0:4], guardians[0], None, staff[0]),
        ("Harbor House", locations[1], patients[4:8], guardians[1], None, staff[1]),
        ("Cedar House", locations[2], patients[8:12], None, patients[8], staff[2]),
    ]
    homes = 0
    for name, location_id, members, guardian_id, owner_patient_id, staff_id in definitions:
        async with engine.begin() as conn:
            home_id = (await conn.execute(text("""
                SELECT id FROM group_homes WHERE agency_id = :agency_id AND location_id = :location_id
            """), {"agency_id": agency.id, "location_id": location_id})).scalar_one_or_none()
            if home_id is None:
                home_id = uuid.uuid4()
                await conn.execute(text("""
                    INSERT INTO group_homes (
                        id, agency_id, location_id, latitude, longitude, name, capacity, is_active, guardian_id, owner_patient_id
                    ) VALUES (
                        :id, :agency_id, :location_id,
                        (SELECT latitude FROM locations WHERE id = :location_id),
                        (SELECT longitude FROM locations WHERE id = :location_id),
                        :name, 4, true, :guardian_id, :owner_patient_id
                    )
                """), {"id": home_id, "agency_id": agency.id, "location_id": location_id,
                       "name": name, "guardian_id": guardian_id, "owner_patient_id": owner_patient_id})
            for patient_id in members:
                exists = (await conn.execute(text("""
                    SELECT 1 FROM group_home_members
                    WHERE group_home_id = :home_id AND patient_id = :patient_id
                """), {"home_id": home_id, "patient_id": patient_id})).scalar_one_or_none()
                if exists is None:
                    await conn.execute(text("""
                        INSERT INTO group_home_members (id, group_home_id, patient_id)
                        VALUES (:id, :home_id, :patient_id)
                    """), {"id": uuid.uuid4(), "home_id": home_id, "patient_id": patient_id})

            existing_appt = (await conn.execute(text("""
                SELECT id FROM group_home_appointments
                WHERE group_home_id = :home_id AND notes = :notes LIMIT 1
            """), {"home_id": home_id, "notes": f"{SEED_TAG} shared service"})).scalar_one_or_none()
            if existing_appt is None:
                appointment_id = uuid.uuid4()
                start = datetime.now(tz=UTC).replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=homes + 2)
                await conn.execute(text("""
                    INSERT INTO group_home_appointments (
                        id, agency_id, group_home_id, staff_id, service,
                        scheduled_start, scheduled_end, notes, status
                    ) VALUES (
                        :id, :agency_id, :home_id, :staff_id, 'Shared personal care and wellness check',
                        :start, :end, :notes, 'SCHEDULED'
                    )
                """), {"id": appointment_id, "agency_id": agency.id, "home_id": home_id,
                       "staff_id": staff_id, "start": start, "end": start + timedelta(hours=2),
                       "notes": f"{SEED_TAG} shared service"})
                for patient_id in members:
                    await conn.execute(text("""
                        INSERT INTO group_home_appointment_patients (id, group_appointment_id, patient_id)
                        VALUES (:id, :appointment_id, :patient_id)
                    """), {"id": uuid.uuid4(), "appointment_id": appointment_id, "patient_id": patient_id})
            homes += 1
    return homes


async def _replace_showcase_schedule(
    engine: AsyncEngine, agency: Agency, *, locations: list[uuid.UUID], patients: list[uuid.UUID], staff: list[uuid.UUID]
) -> tuple[int, int, int]:
    """Replace one agency's rolling eight-day integration test schedule."""
    async with engine.begin() as conn:
        await conn.execute(text("""
            DELETE FROM appointment_activities aa
            USING appointments a
            WHERE aa.appointment_id = a.id AND a.agency_id = :agency_id AND a.notes LIKE :tag
        """), {"agency_id": agency.id, "tag": f"{SEED_TAG}%"})
        await conn.execute(text("""
            DELETE FROM appointments WHERE agency_id = :agency_id AND notes LIKE :tag
        """), {"agency_id": agency.id, "tag": f"{SEED_TAG}%"})

        now = datetime.now(tz=UTC)
        base = now.replace(hour=0, minute=0, second=0, microsecond=0)
        services = [
            ("Morning personal care", "PCA", 4500),
            ("Medication and wellness check", "PCA", 4500),
            ("Community skills support", "245D", 6750),
            ("Care plan check-in", "CFSS", 4500),
        ]
        slots = [1, 3, 5, 11, 14, 17]
        appointment_count = 0
        visit_count = 0
        evv_count = 0
        for index in range(48):
            service, program, amount = services[index % len(services)]
            day_offset = index // len(slots)
            start = base + timedelta(days=day_offset, hours=slots[index % len(slots)])
            status = "SCHEDULED"
            cancelled_at = None
            cancelled_reason = None
            has_visit = False
            if day_offset == 0 and start < now:
                status = ("COMPLETED", "AWAITING_SIGNATURE", "MISSED", "CANCELLED")[index % 4]
                has_visit = status in {"COMPLETED", "AWAITING_SIGNATURE"}
                if status == "CANCELLED":
                    cancelled_at = start - timedelta(hours=2)
                    cancelled_reason = "Family requested a reschedule."
                elif status == "MISSED":
                    cancelled_at = start + timedelta(minutes=20)
                    cancelled_reason = "Caregiver was unavailable; office notified the family."
            elif day_offset == 0 and index % 3 == 0:
                status = "READY"
            elif day_offset in {2, 5} and index % 13 == 0:
                status = "CANCELLED"
                cancelled_at = now
                cancelled_reason = "Family cancelled the upcoming visit."
            appointment_id = uuid.uuid4()
            staff_id = None if status == "SCHEDULED" and index % 9 == 0 else staff[index % len(staff)]
            await conn.execute(text("""
                INSERT INTO appointments (
                    id, agency_id, patient_id, staff_id, program_type, scheduled_start,
                    scheduled_end, status, location, location_id, notes, cancelled_reason,
                    cancelled_at, billing_status, billing_amount_cents, claim_id
                ) VALUES (
                    :id, :agency_id, :patient_id, :staff_id, :program, :start, :end,
                    :status, :location, :location_id, :notes, :cancelled_reason,
                    :cancelled_at, :billing_status, :amount, :claim_id
                )
            """), {
                "id": appointment_id, "agency_id": agency.id,
                "patient_id": patients[index % len(patients)], "staff_id": staff_id,
                "program": program, "start": start, "end": start + timedelta(hours=1),
                "status": status, "location": LOCATIONS[index % len(locations)][0],
                "location_id": locations[index % len(locations)],
                "notes": f"{SEED_TAG} {service}. Linked demonstration appointment.",
                "cancelled_reason": cancelled_reason, "cancelled_at": cancelled_at,
                "billing_status": (
                    "paid" if status == "COMPLETED"
                    else "cancelled" if status in {"CANCELLED", "MISSED"}
                    else "pending"
                ),
                "amount": amount, "claim_id": f"CG-SHOW-{str(appointment_id)[:8].upper()}",
            })
            appointment_count += 1
            activity_ids: list[uuid.UUID] = []
            for activity_name, minutes in ((service, 35), ("Document care outcomes", 15)):
                activity_id = uuid.uuid4()
                activity_ids.append(activity_id)
                await conn.execute(text("""
                    INSERT INTO appointment_activities (
                        id, appointment_id, agency_id, name, planned_minutes, status, notes
                    ) VALUES (
                        :id, :appointment_id, :agency_id, :name, :minutes, 'PENDING', :notes
                    )
                """), {"id": activity_id, "appointment_id": appointment_id, "agency_id": agency.id,
                       "name": activity_name, "minutes": minutes,
                       "notes": "Showcase activity checklist item."})

            if has_visit and staff_id is not None:
                visit_id = uuid.uuid4()
                visit_status = "COMPLETED" if status == "COMPLETED" else "AWAITING_SIGNATURE"
                await conn.execute(text("""
                    INSERT INTO visits (
                        id, appointment_id, agency_id, staff_id, status, sharing_location,
                        billing_confirmed_at, created_at, updated_at
                    ) VALUES (
                        :id, :appointment_id, :agency_id, :staff_id, :status, false,
                        :billing_confirmed_at, now(), now()
                    )
                """), {"id": visit_id, "appointment_id": appointment_id,
                       "agency_id": agency.id, "staff_id": staff_id, "status": visit_status,
                       "billing_confirmed_at": start + timedelta(hours=1)
                       if status == "AWAITING_SIGNATURE" else None})
                visit_count += 1
                await conn.execute(text("""
                    INSERT INTO evv_records (
                        id, visit_id, agency_id, start_time, start_lat, start_lng,
                        start_accuracy_m, start_device_id, start_verification_status,
                        end_time, end_lat, end_lng, end_accuracy_m, created_at, updated_at
                    ) VALUES (
                        :id, :visit_id, :agency_id, :start, 44.953700, -93.090000,
                        10.0, 'showcase-device', 'VERIFIED',
                        :end, 44.953700, -93.090000, 12.0, now(), now()
                    )
                """), {"id": uuid.uuid4(), "visit_id": visit_id, "agency_id": agency.id,
                       "start": start, "end": start + timedelta(hours=1)})
                evv_count += 1
                for activity_id in activity_ids:
                    await conn.execute(text("""
                        INSERT INTO visit_activity_deliveries (
                            id, visit_id, agency_id, activity_id, status, completed_at, created_at, updated_at
                        ) VALUES (
                            :id, :visit_id, :agency_id, :activity_id, 'DONE', :completed_at, now(), now()
                        )
                    """), {"id": uuid.uuid4(), "visit_id": visit_id, "agency_id": agency.id,
                           "activity_id": activity_id, "completed_at": start + timedelta(hours=1)})
    return appointment_count, visit_count, evv_count


async def _replace_showcase_notifications(engine: AsyncEngine, agency: Agency) -> int:
    """Refresh a current admin inbox without touching real notifications."""
    async with engine.begin() as conn:
        admin_id = (await conn.execute(text("""
            SELECT u.id
            FROM users u
            JOIN user_roles ur ON ur.user_id = u.id
            WHERE ur.agency_id = :agency_id AND ur.role = 'AGENCY_ADMIN'
            ORDER BY u.created_at
            LIMIT 1
        """), {"agency_id": agency.id})).scalar_one()
        await conn.execute(text("""
            DELETE FROM notifications
            WHERE agency_id = :agency_id AND metadata->>'source' = 'showcase_seed'
        """), {"agency_id": agency.id})
        notices = (
            ("APPOINTMENT_READY", "Visit ready for review", "A today visit is ready for caregiver review."),
            ("APPOINTMENT_ASSIGNED", "Caregiver assigned", "An upcoming visit now has an assigned caregiver."),
            ("VISIT_SUBMITTED_FOR_SIGNATURE", "Signature requested", "A completed visit is awaiting patient or guardian signature."),
            ("BILLING_CONFIRMED", "Payment received", "A completed appointment has been marked paid."),
            ("APPOINTMENT_CANCELLED", "Visit cancelled", "A future visit was cancelled by the family."),
            ("GENERIC", "Coverage review", "One upcoming appointment remains open for assignment."),
        )
        now = datetime.now(tz=UTC)
        for index, (kind, title, body) in enumerate(notices):
            read_at = now - timedelta(minutes=index * 15) if index in {0, 3} else None
            await conn.execute(text("""
                INSERT INTO notifications (
                    id, agency_id, recipient_user_id, type, title, body, status,
                    metadata, created_at, read_at
                ) VALUES (
                    :id, :agency_id, :recipient_id, :kind, :title, :body, :status,
                    CAST(:metadata AS jsonb), :created_at, :read_at
                )
            """), {
                "id": uuid.uuid4(), "agency_id": agency.id, "recipient_id": admin_id,
                "kind": kind, "title": title, "body": body,
                "status": "READ" if read_at else "SENT",
                "metadata": '{"source":"showcase_seed"}',
                "created_at": now - timedelta(minutes=index * 15), "read_at": read_at,
            })
    return len(notices)


async def seed(agency_admin_email: str) -> None:
    engine = create_async_engine(settings.effective_database_url, pool_pre_ping=True, connect_args={"statement_cache_size": 0})
    try:
        agency = await _resolve_agency(engine, agency_admin_email)
        staff, patients, guardians = await _ensure_roster(engine, agency)
        locations = await _ensure_locations(engine, agency)
        homes = await _ensure_group_homes(engine, agency, locations=locations, patients=patients, guardians=guardians, staff=staff)
        appointments, visits, evv_records = await _replace_showcase_schedule(
            engine, agency, locations=locations, patients=patients, staff=staff
        )
        notifications = await _replace_showcase_notifications(engine, agency)
        print(f"Seeded {agency.name} ({agency.id}): {len(staff)} staff, {len(patients)} patients, "
              f"{len(guardians)} guardians, {len(locations)} locations, {homes} group homes, "
              f"{appointments} appointments across today + 7 days, {visits} visits, "
              f"{evv_records} EVV records, and {notifications} notifications.")
        print(f"Generated login password: {PASSWORD}")
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed a tenant-scoped QlockCare showcase dataset.")
    parser.add_argument("--agency-admin-email", required=True, help="Existing AGENCY_ADMIN email that identifies the target tenant.")
    args = parser.parse_args()
    asyncio.run(seed(args.agency_admin_email))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
