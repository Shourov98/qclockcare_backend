"""Demo seed for the appointment + visit lifecycle tables.

Creates one appointment per major lifecycle phase so the agency-admin
dashboard, the patient portal, and the staff app all have meaningful
data on first load.

For each in-flight or completed visit the seed populates:
  - One materialized `Visit` row + per-activity `VisitActivityDelivery`
  - One `EVVRecord` row (start + end + GPS + verification_status)
  - 1-2 `VisitNote` rows with realistic free-text bodies
  - (For COMPLETED visits) one `AppointmentSignature` row
  - (For the COMPLETED-and-paid A5) the denormalized `billing_status`,
    `billing_paid_at`, `billing_paid_by_user_id`, and `claim_id` columns

Re-running the script wipes its own rows and re-seeds them with a
deterministic dataset so the dev experience is reproducible.

Run:

    uv run python scripts/seed_appointments.py

What gets created (8 appointments, each with 3 activities):

  A1  SCHEDULED                 today 14:00 - 15:00    Morning meds
  A2  READY                     today 16:00 - 17:00   Vitals check
  A3  IN_PROGRESS               now-30m               Bathing assistance
  A4  AWAITING_SIGNATURE        now-2h                Evening routine
  A5  COMPLETED + paid          yesterday 10          Personal care
  A6  CANCELLED                 2 days ago            Rescheduled by family
  A7  MISSED                    3 days ago            Caregiver no-show
  A8  COMPLETED + disputed      4 days ago            Patient disputed duration

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

# Default activity checklist. Every appointment gets 3 of these; the
# visit-level deliveries then mark each as DONE / NOT_DONE / PENDING
# based on which lifecycle state we're in.
DEFAULT_ACTIVITIES: list[tuple[str, int, str]] = [
    ("Check vitals + record", 10, "BP, HR, temp documented."),
    ("Assist with personal hygiene", 20, "Bathing + dressing."),
    ("Prepare light meal", 15, "Per dietary plan on file."),
]


@dataclass(frozen=True)
class SeededIds:
    agency_id: uuid.UUID
    agency_name: str
    patient_profile_id: uuid.UUID
    patient_user_id: uuid.UUID
    staff_profile_id: uuid.UUID
    staff_user_id: uuid.UUID
    admin_user_id: uuid.UUID


@dataclass(frozen=True)
class SeedResult:
    appointments: int
    activities: int
    visits: int
    deliveries: int
    notes: int
    signatures: int
    evv_records: int


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _make_claim_id(agency_name: str, appt_id: uuid.UUID) -> str:
    """Format `CG-{agency_tag}-{appt_short}` for the appointment's `claim_id`.

    Mirrors the runtime generation in
    `src.modules.appointments.service.create_appointment`.
    """
    tag = "".join(ch for ch in (agency_name or "").upper() if ch.isalnum())[:4] or "AGCY"
    return f"CG-{tag}-{str(appt_id)[:8].upper()}"


async def _require_seeded_ids(engine: AsyncEngine) -> SeededIds:
    """Look up the IDs seeded by `seed_test_user.py`.

    Resolves the agency context the same way the JWT auth flow does:
    the admin's "primary" agency is the AGENCY_ADMIN role row that
    shares an agency with both a `staff_profiles` row for the seeded
    STAFF user and a `patient_profiles` row for the seeded PATIENT
    user. This is the agency whose `/appointments`, `/visits`, and
    `/portal/visits` responses will surface the rows we insert.
    """
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
            agency_name=agency_name,
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
    # optional table doesn't poison the rest of the wipe.
    required = (
        "appointment_signatures",
        "visit_notes",
        "evv_records",
        "visit_activity_deliveries",
        "visits",
        "appointment_activities",
        "appointments",
    )
    optional = (
        "service_verifications",
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

        print("Wiping existing appointments + visits + signatures + EVV + notes…")
        await _wipe(engine)

        print("Inserting 8 appointments across the lifecycle…")
        result = await _insert_appointments(engine, ids=ids)
        return result
    finally:
        await engine.dispose()


async def _insert_appointments(
    engine: AsyncEngine,
    *,
    ids: SeededIds,
) -> SeedResult:
    """Insert 8 appointments + activities + (for active/completed states)
    a materialized `Visit` row + per-activity deliveries + EVVRecord +
    visit notes + signature + billing.
    """
    now = _now()

    appt_specs: list[dict] = [
        # A1 — upcoming, scheduled but not assigned
        {
            "code": "A1",
            "status": "SCHEDULED",
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
            "evv_start_verification": "VERIFIED",
            "evv_end_verification": None,  # not yet ended
            "notes_payload": [
                "Arrived on time. Patient was awake and in good spirits. "
                "Bathing assistance provided without incident.",
            ],
            "program_type": "PCA",
        },
        # A4 — visit ended, awaiting signature
        {
            "code": "A4",
            "status": "AWAITING_SIGNATURE",
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
            "evv_start_verification": "VERIFIED",
            "evv_end_verification": "VERIFIED",
            "billing_confirmed": True,
            "notes_payload": [
                "All activities delivered as planned. Patient ate a full "
                "dinner and was comfortable at handoff.",
                "Caregiver arrived 5 minutes early; weather was clear.",
            ],
            "program_type": "PCA",
        },
        # A5 — fully completed + verified + paid
        {
            "code": "A5",
            "status": "COMPLETED",
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
            "evv_start_verification": "VERIFIED",
            "evv_end_verification": "VERIFIED",
            "billing_confirmed": True,
            "billing_paid": True,  # sets billing_status='paid'
            "with_signature": True,
            "signer_role": "PATIENT",
            "notes_payload": [
                "Visit completed per plan. Patient was cooperative; "
                "BP 128/82, normal range.",
                "Light meal prepared (oatmeal + fruit). Hydration "
                "encouraged. No concerns to report.",
            ],
            "program_type": "PCA",
        },
        # A6 — cancelled
        {
            "code": "A6",
            "status": "CANCELLED",
            "start_delta_h": -48,
            "duration_h": 1,
            "title": "Rescheduled by family",
            "notes": "Family requested cancellation due to medical appointment.",
            "location": "Patient residence — 123 Oak St, Springfield",
            "with_visit": False,
            "cancelled_reason": "Family requested cancellation — patient at clinic.",
            "program_type": "PCA",
        },
        # A7 — missed (caregiver no-show)
        {
            "code": "A7",
            "status": "MISSED",
            "start_delta_h": -72,
            "duration_h": 1,
            "title": "Caregiver no-show",
            "notes": "Caregiver did not arrive. Family contacted the office.",
            "location": "Patient residence — 123 Oak St, Springfield",
            "with_visit": False,
            "cancelled_reason": "Caregiver no-show — assigned backup.",
            "program_type": "PCA",
        },
        # A8 — disputed visit, completed but billing contested
        {
            "code": "A8",
            "status": "COMPLETED",
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
            "evv_start_verification": "PENDING",  # location accuracy was poor
            "evv_end_verification": "FAILED",  # signed off outside geofence
            "billing_confirmed": False,  # billing NOT confirmed (disputed)
            "notes_payload": [
                "Caregiver arrived at scheduled time but left after only "
                "30 minutes. Family called to dispute duration.",
            ],
            "program_type": "PCA",
        },
    ]

    appt_n = 0
    activity_n = 0
    visit_n = 0
    delivery_n = 0
    note_n = 0
    signature_n = 0
    evv_n = 0

    async with engine.begin() as conn:
        for spec in appt_specs:
            appt_id = uuid.uuid4()
            scheduled_start = now + timedelta(hours=spec["start_delta_h"])
            scheduled_end = scheduled_start + timedelta(hours=spec["duration_h"])
            claim_id = _make_claim_id(ids.agency_name, appt_id)

            cancelled_at = None
            cancelled_reason = None
            if spec["status"] == "CANCELLED":
                cancelled_at = scheduled_start
                cancelled_reason = spec.get("cancelled_reason")
            elif spec["status"] == "MISSED":
                cancelled_at = scheduled_start + timedelta(minutes=15)
                cancelled_reason = spec.get("cancelled_reason")

            billing_status = "unpaid"
            billing_paid_at = None
            billing_paid_by_user_id = None
            if spec.get("billing_paid"):
                billing_status = "paid"
                # "Admin" processed the payment — use admin_user_id so the
                # audit trail shows the agency-admin (the role that
                # actually flips the toggle in production).
                billing_paid_at = scheduled_end + timedelta(minutes=2)
                billing_paid_by_user_id = ids.admin_user_id

            await conn.execute(
                text(
                    "INSERT INTO appointments ("
                    "id, agency_id, patient_id, staff_id, "
                    "program_type, "
                    "scheduled_start, scheduled_end, status, "
                    "location, notes, "
                    "cancelled_reason, cancelled_at, "
                    "billing_status, billing_paid_at, billing_paid_by_user_id, "
                    "claim_id, "
                    "created_at, updated_at"
                    ") VALUES ("
                    ":id, :agency, :patient, :staff, "
                    ":program, "
                    ":start, :end, :status, "
                    ":location, :notes, "
                    ":cancelled_reason, :cancelled_at, "
                    ":billing_status, :billing_paid_at, :billing_paid_by, "
                    ":claim_id, "
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
                    "location": spec["location"],
                    "notes": spec["notes"],
                    "cancelled_reason": cancelled_reason,
                    "cancelled_at": cancelled_at,
                    "billing_status": billing_status,
                    "billing_paid_at": billing_paid_at,
                    "billing_paid_by": billing_paid_by_user_id,
                    "claim_id": claim_id,
                },
            )
            appt_n += 1

            # ----- activities (free-text per spec) -----
            activity_ids: list[uuid.UUID] = []
            for name, planned_minutes, notes in DEFAULT_ACTIVITIES:
                aid = uuid.uuid4()
                activity_ids.append(aid)
                await conn.execute(
                    text(
                        "INSERT INTO appointment_activities ("
                        "id, appointment_id, agency_id, "
                        "name, planned_minutes, status, notes, "
                        # `service_type` is a legacy NOT-NULL column the
                        # spec-aligned runtime ORM no longer reads, but
                        # the constraint is still enforced. We pass a
                        # placeholder so the INSERT doesn't blow up. The
                        # new free-text `name` column carries the
                        # human-readable label going forward.
                        "service_type, "
                        "created_at, updated_at"
                        ") VALUES ("
                        ":id, :appt, :agency, "
                        ":name, :planned, 'PENDING', :notes, "
                        ":service_type, "
                        "now(), now()"
                        ")"
                    ),
                    {
                        "id": aid,
                        "appt": appt_id,
                        "agency": ids.agency_id,
                        "name": name,
                        "planned": planned_minutes,
                        "notes": notes,
                        "service_type": "PERSONAL_CARE",
                    },
                )
                activity_n += 1

            # ----- materialized visit (in-progress / completed states) -----
            if not spec.get("with_visit"):
                continue

            visit_id = uuid.uuid4()
            started_at = now - timedelta(minutes=spec["visit_started_min_ago"])
            visit_minutes = spec["visit_duration_min"]
            ended_at = started_at + timedelta(minutes=visit_minutes)

            # Visits table: keep the minimal mirror columns (status,
            # billing_confirmed_at, sharing_location). The GPS / device
            # data lives on `evv_records` (1:1 with the visit).
            await conn.execute(
                text(
                    "INSERT INTO visits ("
                    "id, appointment_id, agency_id, staff_id, "
                    "status, billing_confirmed_at, billing_confirmed_by_user_id, "
                    "sharing_location, "
                    "created_at, updated_at"
                    ") VALUES ("
                    ":id, :appt, :agency, :staff, "
                    ":status, "
                    ":billing_confirmed_at, :billing_confirmed_by, "
                    ":sharing, "
                    "now(), now()"
                    ")"
                ),
                {
                    "id": visit_id,
                    "appt": appt_id,
                    "agency": ids.agency_id,
                    "staff": ids.staff_profile_id,
                    "status": spec["visit_status"],
                    "billing_confirmed_at": (
                        ended_at + timedelta(minutes=1)
                        if spec.get("billing_confirmed")
                        else None
                    ),
                    "billing_confirmed_by": (
                        ids.staff_user_id
                        if spec.get("billing_confirmed")
                        else None
                    ),
                    "sharing": True if spec["visit_status"] == "IN_PROGRESS" else False,
                },
            )
            visit_n += 1

            # ----- EVV record (start + end GPS, one row per visit) -----
            # GPS coordinates: Springfield, MN approximate (matches the
            # legacy seed's lat/lng so existing dashboards don't jump).
            start_lat = 44.9778
            start_lng = -93.2650
            end_lat = 44.9778
            end_lng = -93.2650
            # A8: end coords "outside geofence" (so verification=FAILED).
            if spec["code"] == "A8":
                end_lat = 44.9900
                end_lng = -93.2500

            await conn.execute(
                text(
                    "INSERT INTO evv_records ("
                    "id, visit_id, agency_id, "
                    "start_time, start_lat, start_lng, "
                    "start_accuracy_m, start_device_id, "
                    "start_verification_status, "
                    "end_time, end_lat, end_lng, "
                    "end_accuracy_m, "
                    "created_at, updated_at"
                    ") VALUES ("
                    ":id, :visit, :agency, "
                    ":start_time, :start_lat, :start_lng, "
                    ":start_acc, :device_id, "
                    ":start_verif, "
                    ":end_time, :end_lat, :end_lng, "
                    ":end_acc, "
                    "now(), now()"
                    ")"
                ),
                {
                    "id": uuid.uuid4(),
                    "visit": visit_id,
                    "agency": ids.agency_id,
                    "start_time": started_at,
                    "start_lat": start_lat,
                    "start_lng": start_lng,
                    "start_acc": 12.5,
                    "device_id": "seed-device-001",
                    "start_verif": spec.get("evv_start_verification"),
                    "end_time": ended_at
                    if spec["visit_status"] != "IN_PROGRESS"
                    else None,
                    "end_lat": end_lat
                    if spec["visit_status"] != "IN_PROGRESS"
                    else None,
                    "end_lng": end_lng
                    if spec["visit_status"] != "IN_PROGRESS"
                    else None,
                    "end_acc": 12.5
                    if spec["visit_status"] != "IN_PROGRESS"
                    else None,
                },
            )
            evv_n += 1

            # ----- per-activity delivery records -----
            for aid, status_label in zip(activity_ids, spec.get("delivery_statuses", [])):
                completed_at = (
                    ended_at
                    if status_label in ("DONE", "NOT_DONE", "NOT_APPLICABLE")
                    else None
                )
                completed_by = ids.staff_user_id if completed_at else None
                reason = (
                    "Patient asleep during this step."
                    if status_label == "NOT_DONE"
                    else None
                )
                note = None
                if status_label == "DONE" and spec["code"] == "A5" and aid == activity_ids[0]:
                    note = "BP 128/82, normal range."
                await conn.execute(
                    text(
                        "INSERT INTO visit_activity_deliveries ("
                        "id, visit_id, appointment_service_item_id, "
                        "status, reason, note, "
                        "completed_at, completed_by, "
                        "created_at, updated_at"
                        ") VALUES ("
                        ":id, :visit, :activity, "
                        ":status, :reason, :note, "
                        ":completed_at, :completed_by, "
                        "now(), now()"
                        ")"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "visit": visit_id,
                        "activity": aid,
                        "status": status_label,
                        "reason": reason,
                        "note": note,
                        "completed_at": completed_at,
                        "completed_by": completed_by,
                    },
                )
                delivery_n += 1

            # ----- visit notes (1-2 per in-flight / completed visit) -----
            for i, body in enumerate(spec.get("notes_payload", [])):
                # Spread notes across the visit window.
                offset_min = (i + 1) * (visit_minutes // (len(spec["notes_payload"]) + 1))
                created_at = started_at + timedelta(minutes=offset_min)
                await conn.execute(
                    text(
                        "INSERT INTO visit_notes ("
                        "id, visit_id, author_user_id, body, created_at"
                        ") VALUES ("
                        ":id, :visit, :author, :body, :created_at"
                        ")"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "visit": visit_id,
                        "author": ids.staff_user_id,
                        "body": body,
                        "created_at": created_at,
                    },
                )
                note_n += 1

            # ----- appointment signature (COMPLETED visits only) -----
            if spec.get("with_signature"):
                # Render signer display name from the patient's full name
                # so the wire value matches the runtime helper.
                patient_name_row = (
                    await conn.execute(
                        text(
                            "SELECT u.full_name FROM users u "
                            "JOIN patient_profiles pp ON pp.user_id = u.id "
                            "WHERE pp.id = :p"
                        ),
                        {"p": ids.patient_profile_id},
                    )
                ).first()
                full_name = patient_name_row[0] if patient_name_row else None
                # Spec §9 format: "J. Smith"
                if full_name:
                    parts = full_name.strip().split()
                    if len(parts) >= 2:
                        signer_display = f"{parts[0][0]}. {parts[-1]}"
                    else:
                        signer_display = parts[0] if parts else "Patient"
                else:
                    signer_display = "D. Patient"

                await conn.execute(
                    text(
                        "INSERT INTO appointment_signatures ("
                        "id, visit_id, agency_id, "
                        "signer_user_id, signer_role, "
                        "signer_display_name, signature_image_url, "
                        "signed_at, ip_address, user_agent, "
                        "created_at"
                        ") VALUES ("
                        ":id, :visit, :agency, "
                        ":signer_user, :signer_role, "
                        ":signer_name, :img_url, "
                        ":signed_at, :ip, :ua, "
                        "now()"
                        ")"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "visit": visit_id,
                        "agency": ids.agency_id,
                        "signer_user": ids.patient_user_id,
                        "signer_role": spec.get("signer_role", "PATIENT"),
                        "signer_name": signer_display,
                        "img_url": f"https://storage.qlockcare.dev/signatures/{visit_id}.png",
                        "signed_at": ended_at + timedelta(minutes=5),
                        "ip": "127.0.0.1",
                        "ua": "seed-script/1.0",
                    },
                )
                signature_n += 1

    return SeedResult(
        appointments=appt_n,
        activities=activity_n,
        visits=visit_n,
        deliveries=delivery_n,
        notes=note_n,
        signatures=signature_n,
        evv_records=evv_n,
    )


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> None:
    print("Seeding appointment + visit lifecycle demo data…\n")
    result = asyncio.run(_seed())
    print(
        f"\nDone.\n"
        f"  appointments              = {result.appointments}\n"
        f"  appointment_activities    = {result.activities}\n"
        f"  visits                    = {result.visits}\n"
        f"  visit_activity_deliveries = {result.deliveries}\n"
        f"  visit_notes               = {result.notes}\n"
        f"  appointment_signatures    = {result.signatures}\n"
        f"  evv_records               = {result.evv_records}\n"
    )
    print("Now hit these endpoints to see the data:")
    print("  GET /appointments")
    print("  GET /appointments?status=SCHEDULED")
    print("  GET /appointments?date_from=2026-08-27&date_to=2026-08-27")
    print("  GET /visits")
    print("  GET /portal/visits                (login as patient@qlockcare.dev)")


if __name__ == "__main__":
    main()
