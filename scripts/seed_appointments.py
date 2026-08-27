"""Demo seed for the appointment + visit lifecycle tables.

Matches the legacy 21-status lifecycle that the current DB ships with
(so this works against migrations 0001-0026 — the spec-aligned 5-status
lifecycle in `QlockCare_appointemnt_flow.md` is not yet applied).

Creates one appointment per major lifecycle phase so the agency-admin
dashboard, the patient portal, and the staff app all have meaningful
data on first load. Visits that are in-flight or completed get a
materialized `Visit` row + per-activity deliveries + (for completed
visits) a `ServiceVerification`.

Re-running the script wipes its own rows and re-seeds them with a
deterministic dataset so the dev experience is reproducible.

Run:

    uv run python scripts/seed_appointments.py

What gets created (8 appointments, each with 3 service items):

  A1  SCHEDULED         today 14:00 - 15:00    Morning meds
  A2  SCHEDULED/ASSIGNED today 16:00 - 17:00   Vitals check
  A3  CHECKED_IN / IN_PROGRESS   now-30m       Bathing assistance
  A4  CHECKED_OUT                now-2h        Evening routine
  A5  COMPLETED + VERIFIED       yesterday 10  Personal care
  A6  CANCELLED                  2 days ago    Rescheduled by family
  A7  NO_SHOW                    3 days ago    Caregiver no-show
  A8  COMPLETED + DISPUTED       4 days ago    Patient disputed duration

Dependencies:
  - Run `scripts/seed_test_user.py` first. This script looks up the
    seeded AGENCY_ADMIN, STAFF, and PATIENT profiles by email and uses
    them as the FK targets.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from src.core.config import settings


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
DEV_AGENCY_NAME = "QlockCare Dev Agency"

AGENCY_ADMIN_EMAIL = "admin@qlockcare.dev"
STAFF_EMAIL = "staff@qlockcare.dev"
PATIENT_EMAIL = "patient@qlockcare.dev"

# Default service-item checklist. Every appointment gets 3 of these;
# the visit-level deliveries then mark each as DONE / NOT_DONE / PENDING
# based on which lifecycle state we're in.
#
# `service_type` is the legacy ENUM column. The available labels are:
#   PERSONAL_CARE, HOMEMAKING, RESPITE, SKILLED_NURSING,
#   MENTAL_HEALTH, COUNSELING_INDIVIDUAL, COUNSELING_GROUP
DEFAULT_SERVICE_ITEMS: list[tuple[str, int, str | None]] = [
    # (service_type, planned_minutes, notes)
    ("PERSONAL_CARE", 15, "Bathing + dressing assistance."),
    ("SKILLED_NURSING", 10, "Vitals + medication pass."),
    ("HOMEMAKING", 20, "Light meal prep + tidying."),
]


@dataclass(frozen=True)
class SeededIds:
    agency_id: uuid.UUID
    patient_profile_id: uuid.UUID
    patient_user_id: uuid.UUID
    staff_profile_id: uuid.UUID
    staff_user_id: uuid.UUID
    admin_user_id: uuid.UUID


@dataclass(frozen=True)
class SeedResult:
    appointments: int
    service_items: int
    visits: int
    deliveries: int
    verifications: int


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(tz=UTC)


async def _require_seeded_ids(engine: AsyncEngine) -> SeededIds:
    """Look up the IDs seeded by `seed_test_user.py`.

    Resolves the agency context the same way the JWT auth flow does:
    the admin's "primary" agency is the AGENCY_ADMIN role row that
    shares an agency with both a `staff_profiles` row for the seeded
    STAFF user and a `patient_profiles` row for the seeded PATIENT
    user. This is the agency whose `/appointments`, `/visits`, and
    `/portal/visits` responses will surface the rows we insert.
    """
    # Use a real transaction (`begin`) so the cleanup DELETE below commits.
    async with engine.begin() as conn:
        # Resolve the three seeded users.
        user_rows = (
            await conn.execute(
                text(
                    "SELECT email, id FROM users "
                    "WHERE email IN (:a, :s, :p)"
                ),
                {"a": AGENCY_ADMIN_EMAIL, "s": STAFF_EMAIL, "p": PATIENT_EMAIL},
            )
        ).all()
        if len(user_rows) != 3:
            missing = {
                AGENCY_ADMIN_EMAIL,
                STAFF_EMAIL,
                PATIENT_EMAIL,
            } - {r[0] for r in user_rows}
            raise SystemExit(
                "ERROR: missing one of the seeded users: "
                f"{', '.join(sorted(missing))}. "
                "Run scripts/seed_test_user.py first."
            )
        user_ids = {r[0]: r[1] for r in user_rows}
        admin_user_id = user_ids[AGENCY_ADMIN_EMAIL]
        staff_user_id = user_ids[STAFF_EMAIL]
        patient_user_id = user_ids[PATIENT_EMAIL]

        # Find the agency where ALL THREE have a coordinated presence.
        agency_row = (
            await conn.execute(
                text(
                    """
                    SELECT ur.agency_id, ag.name
                    FROM user_roles ur
                    JOIN agencies ag ON ag.id = ur.agency_id
                    WHERE ur.user_id = :u
                      AND ur.role = 'AGENCY_ADMIN'
                      AND EXISTS (
                          SELECT 1 FROM staff_profiles sp
                          WHERE sp.agency_id = ur.agency_id
                            AND sp.user_id = :s
                      )
                      AND EXISTS (
                          SELECT 1 FROM patient_profiles pp
                          WHERE pp.agency_id = ur.agency_id
                            AND pp.user_id = :p
                      )
                    LIMIT 1
                    """
                ),
                {"u": admin_user_id, "s": staff_user_id, "p": patient_user_id},
            )
        ).first()
        if agency_row is None:
            raise SystemExit(
                "ERROR: no agency found where the seeded admin, staff, "
                "and patient all share a coordinated presence. "
                "Run scripts/seed_test_user.py first."
            )
        agency_id = agency_row[0]
        agency_name = agency_row[1]
        print(f"Using agency {agency_id} ({agency_name!r})")

        # Sanity fix: the admin user can have AGENCY_ADMIN role rows at
        # multiple agencies (e.g. leftover rows from earlier dev runs).
        # The auth middleware's `_pick_primary_role` picks one via
        # stable-sort tiebreak — which can return the wrong agency.
        # Delete any duplicate AGENCY_ADMIN role rows that aren't at
        # our resolved agency so the JWT for this admin lands here.
        deleted = await conn.execute(
            text(
                "DELETE FROM user_roles "
                "WHERE user_id = :u AND role = 'AGENCY_ADMIN' "
                "  AND agency_id IS DISTINCT FROM :a"
            ),
            {"u": admin_user_id, "a": agency_id},
        )
        if deleted.rowcount:
            print(
                f"  removed {deleted.rowcount} stale admin role row(s) "
                "at other agencies"
            )

        staff_profile_row = (
            await conn.execute(
                text(
                    "SELECT id FROM staff_profiles "
                    "WHERE agency_id = :a AND user_id = :u"
                ),
                {"a": agency_id, "u": staff_user_id},
            )
        ).first()
        patient_profile_row = (
            await conn.execute(
                text(
                    "SELECT id FROM patient_profiles "
                    "WHERE agency_id = :a AND user_id = :u"
                ),
                {"a": agency_id, "u": patient_user_id},
            )
        ).first()
        if staff_profile_row is None or patient_profile_row is None:
            raise SystemExit(
                "ERROR: missing staff/patient profile at the resolved "
                "agency. Run scripts/seed_test_user.py first."
            )

        return SeededIds(
            agency_id=agency_id,
            patient_profile_id=patient_profile_row[0],
            patient_user_id=patient_user_id,
            staff_profile_id=staff_profile_row[0],
            staff_user_id=staff_user_id,
            admin_user_id=admin_user_id,
        )


async def _wipe(engine: AsyncEngine) -> None:
    """Clear rows in dependency order.

    We don't touch `patient_profiles`, `staff_profiles`, `agencies`,
    `users`, etc — those are owned by `seed_test_user.py`. We only
    clear the appointment-side rows.
    """
    # Each statement runs in its own short transaction so a missing
    # optional table (e.g. `appointment_confirmations` on legacy schemas)
    # doesn't poison the rest of the wipe.
    required = (
        "service_verifications",
        "visit_activity_deliveries",
        "visits",
        "appointment_activities",
        "appointments",
    )
    optional = (
        "appointment_confirmations",
        "appointment_events",
        "appointment_charges",
    )
    for tbl in required:
        async with engine.begin() as conn:
            await conn.execute(text(f"DELETE FROM {tbl}"))
    for tbl in optional:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(f"DELETE FROM {tbl}"))
        except Exception:
            # Table doesn't exist on this schema version — skip.
            pass


async def _seed() -> SeedResult:
    # Supabase pooler (port 6543) doesn't support prepared statements
    # across transactions, so disable asyncpg's cache.
    engine = create_async_engine(
        settings.effective_database_url,
        pool_pre_ping=True,
        connect_args={"statement_cache_size": 0},
    )
    try:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:
            print(
                f"\nDatabase unreachable: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            print(
                "Make sure Supabase is running (`supabase start`) or "
                "DATABASE_URL points at a reachable Postgres.",
                file=sys.stderr,
            )
            raise SystemExit(2) from exc

        ids = await _require_seeded_ids(engine)
        print(f"  staff={ids.staff_profile_id} "
              f"patient={ids.patient_profile_id}")

        print("Wiping existing appointments + visits + service items…")
        await _wipe(engine)

        print("Inserting 8 appointments across the lifecycle…")
        appt_n, item_n, visit_n, delivery_n, verif_n = await _insert_appointments(
            engine, ids=ids
        )

        return SeedResult(
            appointments=appt_n,
            service_items=item_n,
            visits=visit_n,
            deliveries=delivery_n,
            verifications=verif_n,
        )
    finally:
        await engine.dispose()


async def _insert_appointments(
    engine: AsyncEngine,
    *,
    ids: SeededIds,
) -> tuple[int, int, int, int, int]:
    """Insert 8 appointments + their service items + (for active/completed
    states) a materialized Visit row + per-activity deliveries + (for
    completed states) a ServiceVerification.
    """
    now = _now()

    appt_specs: list[dict] = [
        # A1 — upcoming, scheduled but not assigned
        {
            "code": "A1",
            "status": "SCHEDULED",
            "confirmation_status": None,
            "checked_in_at": None,
            "checked_out_at": None,
            "completed_at": None,
            "start_delta_h": 4,
            "duration_h": 1,
            "title": "Morning medication",
            "notes": "Standard AM medication pass.",
            "location": "Patient residence — 123 Oak St, Springfield",
            "with_visit": False,
            "program_type": "PCA",
        },
        # A2 — ready, staff has accepted the visit
        {
            "code": "A2",
            "status": "READY",
            "confirmation_status": "CONFIRMED",
            "checked_in_at": None,
            "checked_out_at": None,
            "completed_at": None,
            "start_delta_h": 6,
            "duration_h": 1,
            "title": "Vitals check + wound inspection",
            "notes": "Caregiver should arrive 10 min early.",
            "location": "Patient residence — 123 Oak St, Springfield",
            "with_visit": False,
            "program_type": "PCA",
        },
        # A3 — currently in progress, mid-visit
        {
            "code": "A3",
            "status": "IN_PROGRESS",
            "confirmation_status": "CONFIRMED",
            "checked_in_at": now - timedelta(minutes=30),
            "checked_out_at": None,
            "completed_at": None,
            "start_delta_h": -0.5,
            "duration_h": 1,
            "title": "Bathing assistance + vitals",
            "notes": "Mid-visit. Caregiver on-site.",
            "location": "Patient residence — 123 Oak St, Springfield",
            "with_visit": True,
            "visit_status": "IN_PROGRESS",
            "visit_started_min_ago": 30,
            "visit_duration_min": 60,
            "delivery_statuses": ["DONE", "DONE", "PENDING"],
            "program_type": "PCA",
        },
        # A4 — visit ended, awaiting signature
        {
            "code": "A4",
            "status": "AWAITING_SIGNATURE",
            "confirmation_status": "CONFIRMED",
            "checked_in_at": now - timedelta(minutes=180),
            "checked_out_at": now - timedelta(minutes=120),
            "completed_at": None,
            "start_delta_h": -3,
            "duration_h": 1,
            "title": "Evening routine + dinner prep",
            "notes": "Caregiver submitted End Task. Awaiting patient signature.",
            "location": "Patient residence — 123 Oak St, Springfield",
            "with_visit": True,
            "visit_status": "AWAITING_SIGNATURE",
            "visit_started_min_ago": 180,
            "visit_duration_min": 60,
            "delivery_statuses": ["DONE", "DONE", "DONE"],
            "program_type": "PCA",
        },
        # A5 — fully completed + verified
        {
            "code": "A5",
            "status": "COMPLETED",
            "confirmation_status": "CONFIRMED",
            "checked_in_at": now - timedelta(hours=26),
            "checked_out_at": now - timedelta(hours=25),
            "completed_at": now - timedelta(hours=25),
            "start_delta_h": -26,
            "duration_h": 1,
            "title": "Personal care visit",
            "notes": "Signed off yesterday morning. All activities delivered.",
            "location": "Patient residence — 123 Oak St, Springfield",
            "with_visit": True,
            "visit_status": "COMPLETED",
            "visit_started_min_ago": 60 * 26,
            "visit_duration_min": 60,
            "delivery_statuses": ["DONE", "DONE", "DONE"],
            "with_verification": True,
            "verification_status": "VERIFIED",
            "verification_comment": "All activities delivered as planned.",
            "program_type": "PCA",
        },
        # A6 — cancelled
        {
            "code": "A6",
            "status": "CANCELLED",
            "confirmation_status": "DECLINED",
            "checked_in_at": None,
            "checked_out_at": None,
            "completed_at": None,
            "start_delta_h": -48,
            "duration_h": 1,
            "title": "Rescheduled by family",
            "notes": "Family requested cancellation due to medical appointment.",
            "location": "Patient residence — 123 Oak St, Springfield",
            "with_visit": False,
            "cancelled_reason": "Family requested cancellation — patient at clinic.",
            "program_type": "PCA",
        },
        # A7 — no-show (legacy enum value) → MISSED in spec enum
        {
            "code": "A7",
            "status": "MISSED",
            "confirmation_status": "CONFIRMED",
            "checked_in_at": None,
            "checked_out_at": None,
            "completed_at": None,
            "start_delta_h": -72,
            "duration_h": 1,
            "title": "Caregiver no-show",
            "notes": "Caregiver did not arrive. Family contacted the office.",
            "location": "Patient residence — 123 Oak St, Springfield",
            "with_visit": False,
            "program_type": "PCA",
        },
        # A8 — disputed visit, marked COMPLETED but verification disputed
        {
            "code": "A8",
            "status": "COMPLETED",
            "confirmation_status": "CONFIRMED",
            "checked_in_at": now - timedelta(hours=97),
            "checked_out_at": now - timedelta(hours=96),
            "completed_at": now - timedelta(hours=96),
            "start_delta_h": -96,
            "duration_h": 1,
            "title": "Visit duration disputed",
            "notes": "Family reports visit was shorter than billed.",
            "location": "Patient residence — 123 Oak St, Springfield",
            "with_visit": True,
            "visit_status": "COMPLETED",
            "visit_started_min_ago": 60 * 97,
            "visit_duration_min": 30,  # short — patient disputes this
            "delivery_statuses": ["DONE", "DONE", "NOT_DONE"],
            "with_verification": True,
            "verification_status": "DISPUTED",
            "verification_dispute_reason": "SERVICE_NOT_COMPLETED",
            "verification_comment": "Family says only 30 minutes were billed vs 60 scheduled.",
            "program_type": "PCA",
        },
    ]

    appt_n = 0
    item_n = 0
    visit_n = 0
    delivery_n = 0
    verif_n = 0

    async with engine.begin() as conn:
        for spec in appt_specs:
            appt_id = uuid.uuid4()
            scheduled_start = now + timedelta(hours=spec["start_delta_h"])
            scheduled_end = scheduled_start + timedelta(hours=spec["duration_h"])

            cancelled_at = None
            cancelled_reason = None
            if spec["status"] == "CANCELLED":
                cancelled_at = scheduled_start
                cancelled_reason = spec.get("cancelled_reason")

            confirmed_at = None
            if spec.get("confirmation_status") == "CONFIRMED":
                confirmed_at = scheduled_start - timedelta(hours=12)
            # DECLINED appointments also have a confirmed_at (the
            # timestamp of the decline decision).
            elif spec.get("confirmation_status") == "DECLINED":
                confirmed_at = scheduled_start - timedelta(hours=12)

            await conn.execute(
                text(
                    "INSERT INTO appointments ("
                    "id, agency_id, patient_id, staff_id, "
                    "program_type, "
                    "scheduled_start, scheduled_end, status, "
                    "confirmation_status, confirmed_at, confirmation_note, "
                    "checked_in_at, checked_out_at, completed_at, "
                    "location, notes, cancelled_reason, cancelled_at, "
                    "created_at, updated_at"
                    ") VALUES ("
                    ":id, :agency, :patient, :staff, "
                    ":program, "
                    ":start, :end, :status, "
                    ":conf_status, :confirmed_at, :conf_note, "
                    ":ci_at, :co_at, :completed_at, "
                    ":location, :notes, :cancelled_reason, :cancelled_at, "
                    "now(), now()"
                    ")"
                ),
                {
                    "id": appt_id,
                    "agency": ids.agency_id,
                    "patient": ids.patient_profile_id,
                    "staff": ids.staff_profile_id,
                    "program": spec.get("program_type"),
                    "start": scheduled_start,
                    "end": scheduled_end,
                    "status": spec["status"],
                    "conf_status": spec.get("confirmation_status"),
                    "confirmed_at": confirmed_at,
                    "conf_note": None,
                    "ci_at": spec.get("checked_in_at"),
                    "co_at": spec.get("checked_out_at"),
                    "completed_at": spec.get("completed_at"),
                    "location": spec["location"],
                    "notes": spec["notes"],
                    "cancelled_reason": cancelled_reason,
                    "cancelled_at": cancelled_at,
                },
            )
            appt_n += 1

            # ----- service items (legacy ENUM-based checklist) -----
            service_item_ids: list[uuid.UUID] = []
            for service_type, planned_minutes, notes in DEFAULT_SERVICE_ITEMS:
                si_id = uuid.uuid4()
                service_item_ids.append(si_id)
                await conn.execute(
                    text(
                        "INSERT INTO appointment_activities ("
                        "id, appointment_id, agency_id, "
                        "service_type, planned_minutes, status, notes, "
                        "name, created_at, updated_at"
                        ") VALUES ("
                        ":id, :appt, :agency, "
                        ":stype, :planned, 'PENDING', :notes, "
                        ":name, now(), now()"
                        ")"
                    ),
                    {
                        "id": si_id,
                        "appt": appt_id,
                        "agency": ids.agency_id,
                        "stype": service_type,
                        "planned": planned_minutes,
                        "notes": notes,
                        # The runtime ORM reads `name` (free-text per
                        # the spec) so set it from the legacy enum label.
                        "name": service_type.replace("_", " ").title(),
                    },
                )
                item_n += 1

            # ----- materialized visit (in-progress / completed states) -----
            if spec.get("with_visit"):
                visit_id = uuid.uuid4()
                started_at = now - timedelta(
                    minutes=spec["visit_started_min_ago"]
                )
                ended_at = started_at + timedelta(
                    minutes=spec["visit_duration_min"]
                )
                duration_seconds = int(
                    (ended_at - started_at).total_seconds()
                )

                await conn.execute(
                    text(
                        "INSERT INTO visits ("
                        "id, appointment_id, agency_id, staff_id, "
                        "status, "
                        "check_in_time, check_in_lat, check_in_lng, "
                        "check_in_accuracy_m, check_in_device_id, "
                        "check_in_address_match, check_in_distance_from_location_m, "
                        "check_out_time, check_out_lat, check_out_lng, "
                        "check_out_accuracy_m, "
                        "duration_seconds, "
                        "created_at, updated_at"
                        ") VALUES ("
                        ":id, :appt, :agency, :staff, "
                        ":status, "
                        ":ci_time, 44.9778, -93.2650, "
                        "12.5, 'seed-device-001', "
                        "true, 25, "
                        ":co_time, 44.9778, -93.2650, "
                        "12.5, "
                        ":duration, "
                        "now(), now()"
                        ")"
                    ),
                    {
                        "id": visit_id,
                        "appt": appt_id,
                        "agency": ids.agency_id,
                        "staff": ids.staff_profile_id,
                        "status": spec["visit_status"],
                        "ci_time": started_at,
                        "co_time": ended_at if spec["visit_status"] != "IN_PROGRESS" else None,
                        "duration": duration_seconds if spec["visit_status"] != "IN_PROGRESS" else None,
                    },
                )
                visit_n += 1

                # ----- per-activity delivery records (visit_service_items) -----
                statuses = spec.get("delivery_statuses", [])
                for si_id, status in zip(service_item_ids, statuses):
                    completed_at = (
                        ended_at
                        if status in ("DONE", "NOT_DONE", "NOT_APPLICABLE")
                        else None
                    )
                    completed_by = ids.staff_user_id if completed_at else None
                    reason = (
                        "Patient asleep during this step."
                        if status == "NOT_DONE"
                        else None
                    )
                    note = None
                    if (
                        status == "DONE"
                        and spec["code"] == "A5"
                        and si_id == service_item_ids[0]
                    ):
                        # Annotate the COMPLETED visit's first activity
                        note = "BP 128/82, normal range."
                    await conn.execute(
                        text(
                            "INSERT INTO visit_activity_deliveries ("
                            "id, visit_id, appointment_service_item_id, "
                            "status, reason, note, completed_at, "
                            "completed_by, created_at, updated_at"
                            ") VALUES ("
                            ":id, :visit, :si, "
                            ":status, :reason, :note, :completed_at, "
                            ":completed_by, now(), now()"
                            ")"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "visit": visit_id,
                            "si": si_id,
                            "status": status,
                            "reason": reason,
                            "note": note,
                            "completed_at": completed_at,
                            "completed_by": completed_by,
                        },
                    )
                    delivery_n += 1

                # ----- service verification (COMPLETED + verified/disputed) -----
                if spec.get("with_verification"):
                    await conn.execute(
                        text(
                            "INSERT INTO service_verifications ("
                            "id, visit_id, agency_id, "
                            "verified_by, verifier_role, "
                            "status, dispute_reason_code, comment, "
                            "created_at"
                            ") VALUES ("
                            ":id, :visit, :agency, "
                            ":verifier, 'PATIENT', "
                            ":status, :reason, :comment, "
                            ":created_at"
                            ")"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "visit": visit_id,
                            "agency": ids.agency_id,
                            "verifier": ids.patient_user_id,
                            "status": spec["verification_status"],
                            "reason": spec.get("verification_dispute_reason"),
                            "comment": spec.get("verification_comment"),
                            "created_at": ended_at + timedelta(minutes=10),
                        },
                    )
                    verif_n += 1

    return appt_n, item_n, visit_n, delivery_n, verif_n


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> None:
    print("Seeding appointment + visit lifecycle demo data…\n")
    result = asyncio.run(_seed())
    print(
        f"\nDone.\n"
        f"  appointments              = {result.appointments}\n"
        f"  appointment_service_items = {result.service_items}\n"
        f"  visits                    = {result.visits}\n"
        f"  visit_service_items       = {result.deliveries}\n"
        f"  service_verifications     = {result.verifications}\n"
    )
    print("Now hit these endpoints to see the data:")
    print("  GET /appointments")
    print("  GET /appointments?status=SCHEDULED")
    print("  GET /appointments?status=ASSIGNED")
    print("  GET /visits")
    print("  GET /portal/visits                (login as patient@qlockcare.dev)")


if __name__ == "__main__":
    main()
