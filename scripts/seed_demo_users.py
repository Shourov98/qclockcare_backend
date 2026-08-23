"""Demo users seed — provisions the role-roster used by the Postman
collection's demo requests.

Unlike `seed_test_user.py` (which creates the canonical 4 users:
super, agency-admin, staff, patient), this script adds the additional
STAFF / PATIENT / GUARDIAN / PLATFORM_ADMIN rows that the broader
Postman collection references (e.g. `staff6@qlockcare.dev`).

The script is **strictly additive**: it never deletes or modifies
existing rows. If an email already exists it is skipped (and reported
in the summary), so re-running is safe.

Run:

    uv run python scripts/seed_demo_users.py

What gets created
-----------------

STAFF       staff1..staff6@qlockcare.dev          / StaffDevPass123!
            staff_profile rows for each (linked to "QlockCare Dev Agency")

PATIENT     patient1..patient8@qlockcare.dev      / PatientDevPass123!
            patient_profile rows for each (linked to "QlockCare Dev Agency")

GUARDIAN    guardian1..guardian3@qlockcare.dev    / GuardianDevPass123!
            guardian_profile rows for each (linked to "QlockCare Dev Agency")
            plus a `patient_guardian_relationships` row linking each
            guardian to patient1 so the GUARDIAN login actually has
            someone to look at in the mobile app.

PLATFORM_ADMIN
            platform@qlockcare.dev                / PlatformDevPass123!
            role=PLATFORM_ADMIN, agency_id=NULL,
            admin_scopes = {AGENCIES, CLINICAL, SUPPORT}

All accounts are created with `status=ACTIVE`, `email_verified_at=now()`,
and `must_change_password=false` so a developer can log in immediately.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from src.core.config import settings
from src.core.security import hash_password
from src.shared.domain.enums import AdminScope


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

DEV_AGENCY_NAME = "QlockCare Dev Agency"

STAFF_PASSWORD = "StaffDevPass123!"
PATIENT_PASSWORD = "PatientDevPass123!"
GUARDIAN_PASSWORD = "GuardianDevPass123!"
PLATFORM_ADMIN_PASSWORD = "PlatformDevPass123!"

# All staff/patient/guardian users attach to the primary dev agency so
# AGENCY_ADMIN / mobile-role queries have something to filter on.
STAFF_EMAILS: list[str] = [f"staff{i}@qlockcare.dev" for i in range(1, 7)]
PATIENT_EMAILS: list[str] = [f"patient{i}@qlockcare.dev" for i in range(1, 9)]
GUARDIAN_EMAILS: list[str] = [f"guardian{i}@qlockcare.dev" for i in range(1, 4)]

PLATFORM_ADMIN_EMAIL = "platform@qlockcare.dev"
PLATFORM_ADMIN_NAME = "Dev Platform Admin"

# Each guardian gets linked to this patient (so the mobile app has data).
PRIMARY_PATIENT_EMAIL = "patient1@qlockcare.dev"


@dataclass(frozen=True)
class SeedResult:
    staff_created: int
    staff_skipped: int
    patient_created: int
    patient_skipped: int
    guardian_created: int
    guardian_skipped: int
    platform_admin_created: bool
    guardian_relationships_created: int


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
async def _fetch_dev_agency_id(engine: AsyncEngine) -> uuid.UUID:
    """Return the id of the QlockCare Dev Agency row. Hard-fail if absent."""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT id FROM agencies WHERE name = :n"),
                {"n": DEV_AGENCY_NAME},
            )
        ).first()
    if row is None:
        raise SystemExit(
            f"ERROR: agency {DEV_AGENCY_NAME!r} not found.\n"
            "Run scripts/seed_test_user.py first — that script provisions "
            "the agency this script depends on."
        )
    return row[0]


async def _user_exists(engine: AsyncEngine, email: str) -> bool:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT 1 FROM users WHERE email = :e LIMIT 1"),
                {"e": email},
            )
        ).first()
    return row is not None


async def _create_user_with_role(
    engine: AsyncEngine,
    *,
    email: str,
    password: str,
    full_name: str,
    role: str,
    agency_id: uuid.UUID | None,
    phone: str | None = None,
) -> uuid.UUID:
    """Insert the user + a user_roles row. Assumes `_user_exists` was checked."""
    user_id = uuid.uuid4()
    pw_hash = hash_password(password)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users "
                "(id, email, password_hash, full_name, phone, status, "
                " email_verified_at, must_change_password, failed_login_attempts) "
                "VALUES (:id, :email, :pw, :name, :phone, 'ACTIVE', now(), false, 0)"
            ),
            {
                "id": user_id,
                "email": email,
                "pw": pw_hash,
                "name": full_name,
                "phone": phone,
            },
        )
        await conn.execute(
            text(
                "INSERT INTO user_roles (id, user_id, agency_id, role) "
                "VALUES (:id, :u, :a, :r)"
            ),
            {"id": uuid.uuid4(), "u": user_id, "a": agency_id, "r": role},
        )
    return user_id


async def _create_staff_profile(
    engine: AsyncEngine,
    *,
    agency_id: uuid.UUID,
    user_id: uuid.UUID,
    staff_code: str,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO staff_profiles "
                "(id, agency_id, user_id, staff_code, status, hired_at) "
                "VALUES (:id, :a, :u, :code, 'ACTIVE', current_date)"
            ),
            {
                "id": uuid.uuid4(),
                "a": agency_id,
                "u": user_id,
                "code": staff_code,
            },
        )


async def _create_patient_profile(
    engine: AsyncEngine,
    *,
    agency_id: uuid.UUID,
    user_id: uuid.UUID,
    patient_code: str,
    dob: str,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO patient_profiles "
                "(id, agency_id, user_id, patient_code, status, "
                " date_of_birth, admitted_at) "
                "VALUES (:id, :a, :u, :code, 'ACTIVE', :dob, current_date)"
            ),
            {
                "id": uuid.uuid4(),
                "a": agency_id,
                "u": user_id,
                "code": patient_code,
                "dob": dob,
            },
        )


async def _create_guardian_profile(
    engine: AsyncEngine,
    *,
    agency_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO guardian_profiles "
                "(id, agency_id, user_id, status, contact_phone, contact_email) "
                "VALUES (:id, :a, :u, 'ACTIVE', :phone, :email)"
            ),
            {
                "id": uuid.uuid4(),
                "a": agency_id,
                "u": user_id,
                "phone": "+1-555-0000",
                "email": "guardian@example.com",
            },
        )


async def _link_guardian_to_patient1(
    engine: AsyncEngine, *, agency_id: uuid.UUID
) -> int:
    """Create one PATIENT relationship between each guardian and patient1.

    Returns the number of relationships created. Skips silently if a
    given (patient, guardian, relationship) tuple already exists.
    """
    async with engine.connect() as conn:
        patient_row = (
            await conn.execute(
                text(
                    "SELECT id FROM patient_profiles "
                    "WHERE user_id = (SELECT id FROM users WHERE email = :e)"
                ),
                {"e": PRIMARY_PATIENT_EMAIL},
            )
        ).first()
        if patient_row is None:
            return 0
        patient_id = patient_row[0]

        guardian_rows = (
            await conn.execute(
                text(
                    "SELECT gp.id FROM guardian_profiles gp "
                    "JOIN users u ON u.id = gp.user_id "
                    "WHERE u.email LIKE 'guardian%@qlockcare.dev'"
                )
            )
        ).all()
    if not guardian_rows:
        return 0

    created = 0
    for g_row in guardian_rows:
        guardian_id = g_row[0]
        async with engine.begin() as conn:
            # Check existence first (unique constraint).
            existing = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM patient_guardian_relationships "
                        "WHERE patient_id = :p AND guardian_id = :g "
                        "  AND relationship_type = 'EMERGENCY_CONTACT'"
                    ),
                    {"p": patient_id, "g": guardian_id},
                )
            ).first()
            if existing is not None:
                continue
            await conn.execute(
                text(
                    "INSERT INTO patient_guardian_relationships "
                    "(id, agency_id, patient_id, guardian_id, "
                    " relationship_type, is_legal, valid_from) "
                    "VALUES (:id, :a, :p, :g, "
                    " 'EMERGENCY_CONTACT', true, current_date)"
                ),
                {
                    "id": uuid.uuid4(),
                    "a": agency_id,
                    "p": patient_id,
                    "g": guardian_id,
                },
            )
            created += 1
    return created


async def _create_platform_admin(engine: AsyncEngine) -> bool:
    """Create the PLATFORM_ADMIN user + admin_scopes rows.

    Returns True if created, False if the user already existed.
    """
    if await _user_exists(engine, PLATFORM_ADMIN_EMAIL):
        return False
    user_id = uuid.uuid4()
    pw_hash = hash_password(PLATFORM_ADMIN_PASSWORD)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users "
                "(id, email, password_hash, full_name, status, "
                " email_verified_at, must_change_password) "
                "VALUES (:id, :e, :pw, :n, 'ACTIVE', now(), false)"
            ),
            {"id": user_id, "e": PLATFORM_ADMIN_EMAIL, "pw": pw_hash, "n": PLATFORM_ADMIN_NAME},
        )
        await conn.execute(
            text(
                "INSERT INTO user_roles (id, user_id, agency_id, role) "
                "VALUES (:id, :u, NULL, 'PLATFORM_ADMIN')"
            ),
            {"id": uuid.uuid4(), "u": user_id},
        )
        for scope in (
            AdminScope.AGENCIES,
            AdminScope.CLINICAL,
            AdminScope.SUPPORT,
        ):
            await conn.execute(
                text(
                    "INSERT INTO admin_scopes (user_id, scope_name, granted_by) "
                    "VALUES (:u, :s, NULL)"
                ),
                {"u": user_id, "s": scope.value},
            )
    return True


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
async def _seed() -> SeedResult:
    engine = create_async_engine(
        settings.effective_database_url,
        pool_pre_ping=True,
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
            raise SystemExit(2) from exc

        agency_id = await _fetch_dev_agency_id(engine)

        # ---------- PLATFORM_ADMIN enum bootstrap ----------
        # The original UserRole migration didn't include PLATFORM_ADMIN
        # (it was added later as part of the admin-scope RBAC plan).
        # Add it on the fly so the `_create_platform_admin` step below
        # doesn't blow up with `invalid input value for enum user_role`.
        # `ADD VALUE IF NOT EXISTS` is idempotent.
        async with engine.begin() as conn:
            await conn.execute(
                text("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'PLATFORM_ADMIN'")
            )

        # ---------- STAFF ----------
        staff_created = 0
        staff_skipped = 0
        for idx, email in enumerate(STAFF_EMAILS, start=1):
            if await _user_exists(engine, email):
                staff_skipped += 1
                continue
            user_id = await _create_user_with_role(
                engine,
                email=email,
                password=STAFF_PASSWORD,
                full_name=f"Demo Staff {idx}",
                role="STAFF",
                agency_id=agency_id,
                phone=f"+1-555-01{idx:02d}",
            )
            await _create_staff_profile(
                engine,
                agency_id=agency_id,
                user_id=user_id,
                staff_code=f"STF-DEMO{idx:02d}",
            )
            staff_created += 1

        # ---------- PATIENT ----------
        patient_created = 0
        patient_skipped = 0
        # Realistic spread of dates of birth (1945-01-01 .. 2010-12-31).
        # Stored as `datetime.date` so asyncpg binds correctly without an
        # inline `::date` cast (which would break the parameter parser).
        dobs: list[date] = [
            date(1945, 3, 12), date(1952, 7, 8), date(1961, 11, 23),
            date(1968, 4, 30), date(1975, 9, 15), date(1982, 1, 22),
            date(1991, 6, 17), date(2010, 12, 4),
        ]
        for idx, email in enumerate(PATIENT_EMAILS, start=1):
            if await _user_exists(engine, email):
                patient_skipped += 1
                continue
            user_id = await _create_user_with_role(
                engine,
                email=email,
                password=PATIENT_PASSWORD,
                full_name=f"Demo Patient {idx}",
                role="PATIENT",
                agency_id=agency_id,
                phone=f"+1-555-02{idx:02d}",
            )
            await _create_patient_profile(
                engine,
                agency_id=agency_id,
                user_id=user_id,
                patient_code=f"PAT-DEMO{idx:02d}",
                dob=dobs[idx - 1],
            )
            patient_created += 1

        # ---------- GUARDIAN ----------
        guardian_created = 0
        guardian_skipped = 0
        for idx, email in enumerate(GUARDIAN_EMAILS, start=1):
            if await _user_exists(engine, email):
                guardian_skipped += 1
                continue
            user_id = await _create_user_with_role(
                engine,
                email=email,
                password=GUARDIAN_PASSWORD,
                full_name=f"Demo Guardian {idx}",
                role="GUARDIAN",
                agency_id=agency_id,
                phone=f"+1-555-03{idx:02d}",
            )
            await _create_guardian_profile(
                engine, agency_id=agency_id, user_id=user_id
            )
            guardian_created += 1

        # ---------- Guardian <-> Patient relationships ----------
        rels_created = await _link_guardian_to_patient1(
            engine, agency_id=agency_id
        )

        # ---------- PLATFORM_ADMIN ----------
        platform_created = await _create_platform_admin(engine)

        return SeedResult(
            staff_created=staff_created,
            staff_skipped=staff_skipped,
            patient_created=patient_created,
            patient_skipped=patient_skipped,
            guardian_created=guardian_created,
            guardian_skipped=guardian_skipped,
            platform_admin_created=platform_created,
            guardian_relationships_created=rels_created,
        )
    finally:
        await engine.dispose()


def main() -> None:
    print("Seeding demo users (additive — never deletes)…\n")
    result = asyncio.run(_seed())
    print("Done.\n")
    print(f"  STAFF           created={result.staff_created}   skipped={result.staff_skipped}")
    print(f"  PATIENT         created={result.patient_created} skipped={result.patient_skipped}")
    print(f"  GUARDIAN        created={result.guardian_created} skipped={result.guardian_skipped}")
    print(
        f"  PLATFORM_ADMIN  created={result.platform_admin_created} "
        f"(email={PLATFORM_ADMIN_EMAIL}, "
        f"scopes=AGENCIES+CLINICAL+SUPPORT)"
    )
    print(
        f"  patient_guardian_relationships created={result.guardian_relationships_created}"
    )
    print("\nLogins:")
    print(f"  STAFF          : staff1..staff6@qlockcare.dev / {STAFF_PASSWORD}")
    print(f"  PATIENT        : patient1..patient8@qlockcare.dev / {PATIENT_PASSWORD}")
    print(f"  GUARDIAN       : guardian1..guardian3@qlockcare.dev / {GUARDIAN_PASSWORD}")
    print(f"  PLATFORM_ADMIN : {PLATFORM_ADMIN_EMAIL} / {PLATFORM_ADMIN_PASSWORD}")


if __name__ == "__main__":
    main()