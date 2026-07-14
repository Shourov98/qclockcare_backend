"""Idempotent dev seed — provisions the 4 known users the Postman collection
expects.

Creates (or no-ops if already present):

  - Agency "QlockCare Dev Agency"
  - SUPER_ADMIN      super@qlockcare.dev       / SuperDevPass123!
  - AGENCY_ADMIN     admin@qlockcare.dev       / AdminDevPass123!
  - STAFF (linked to a staff_profile)  staff@qlockcare.dev  / StaffDevPass123!
  - PATIENT (linked to a patient_profile) patient@qlockcare.dev / PatientDevPass123!

Run:

    uv run python scripts/seed_test_user.py

The script is idempotent — re-running is safe. It clears existing role
rows for the seeded users before re-inserting so the SUPER_ADMIN ↔
agency constraint never trips on a partial state.

The credentials are also written to `.env.test` so a developer doesn't
have to read the script to know the logins.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from src.core.config import settings
from src.core.security import hash_password

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

DEV_AGENCY_NAME = "QlockCare Dev Agency"
DEV_AGENCY_TIMEZONE = "America/Chicago"

SUPER_ADMIN_EMAIL = "super@qlockcare.dev"
SUPER_ADMIN_PASSWORD = "SuperDevPass123!"
SUPER_ADMIN_NAME = "Dev Super Admin"

AGENCY_ADMIN_EMAIL = "admin@qlockcare.dev"
AGENCY_ADMIN_PASSWORD = "AdminDevPass123!"
AGENCY_ADMIN_NAME = "Dev Agency Admin"

STAFF_EMAIL = "staff@qlockcare.dev"
STAFF_PASSWORD = "StaffDevPass123!"
STAFF_NAME = "Dev Staff"
STAFF_CODE = "STF-DEV001"

PATIENT_EMAIL = "patient@qlockcare.dev"
PATIENT_PASSWORD = "PatientDevPass123!"
PATIENT_NAME = "Dev Patient"
PATIENT_CODE = "PAT-DEV001"


@dataclass(frozen=True)
class SeededIds:
    agency_id: uuid.UUID
    super_admin_id: uuid.UUID
    agency_admin_id: uuid.UUID
    staff_user_id: uuid.UUID
    staff_profile_id: uuid.UUID
    patient_user_id: uuid.UUID
    patient_profile_id: uuid.UUID


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

async def _fetch_or_create_agency(engine: AsyncEngine) -> uuid.UUID:
    """Return the id of an agency named DEV_AGENCY_NAME, creating if needed."""
    async with engine.begin() as conn:
        existing = (
            await conn.execute(
                text("SELECT id FROM agencies WHERE name = :n"),
                {"n": DEV_AGENCY_NAME},
            )
        ).first()
        if existing is not None:
            return cast("uuid.UUID", existing[0])
        new_id = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO agencies (id, name, timezone, status) "
                "VALUES (:id, :name, :tz, 'ACTIVE')"
            ),
            {"id": new_id, "name": DEV_AGENCY_NAME, "tz": DEV_AGENCY_TIMEZONE},
        )
        return new_id


async def _fetch_or_create_user(
    engine: AsyncEngine,
    *,
    email: str,
    password: str,
    full_name: str,
    phone: str | None = None,
) -> uuid.UUID:
    """Return the id of a user, creating (with hashed password) if needed.

    Sets `email_verified_at = now()` and `status = 'ACTIVE'` so the user
    can log in immediately without an OTP step.
    """
    async with engine.begin() as conn:
        existing = (
            await conn.execute(
                text("SELECT id FROM users WHERE email = :e"),
                {"e": email},
            )
        ).first()
        if existing is not None:
            return cast("uuid.UUID", existing[0])
        new_id = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO users "
                "(id, email, password_hash, full_name, phone, status, email_verified_at) "
                "VALUES (:id, :e, :pw, :n, :ph, 'ACTIVE', now())"
            ),
            {
                "id": new_id,
                "e": email,
                "pw": hash_password(password),
                "n": full_name,
                "ph": phone,
            },
        )
        return new_id


async def _replace_role(
    engine: AsyncEngine,
    *,
    user_id: uuid.UUID,
    role: str,
    agency_id: uuid.UUID | None,
) -> None:
    """DELETE-then-INSERT a role row.

    The `user_roles` table has a `(user_id, role, agency_id)` unique
    constraint, so we can't naively re-INSERT. We DELETE first.
    """
    async with engine.begin() as conn:
        if agency_id is None:
            await conn.execute(
                text("DELETE FROM user_roles WHERE user_id = :u AND role = :r"),
                {"u": user_id, "r": role},
            )
        else:
            await conn.execute(
                text(
                    "DELETE FROM user_roles "
                    "WHERE user_id = :u AND role = :r AND agency_id = :a"
                ),
                {"u": user_id, "r": role, "a": agency_id},
            )
        await conn.execute(
            text(
                "INSERT INTO user_roles (id, user_id, agency_id, role) "
                "VALUES (:id, :u, :a, :r)"
            ),
            {"id": uuid.uuid4(), "u": user_id, "a": agency_id, "r": role},
        )


async def _fetch_or_create_staff_profile(
    engine: AsyncEngine,
    *,
    agency_id: uuid.UUID,
    user_id: uuid.UUID,
) -> uuid.UUID:
    async with engine.begin() as conn:
        existing = (
            await conn.execute(
                text(
                    "SELECT id FROM staff_profiles "
                    "WHERE agency_id = :a AND user_id = :u"
                ),
                {"a": agency_id, "u": user_id},
            )
        ).first()
        if existing is not None:
            return cast("uuid.UUID", existing[0])
        new_id = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO staff_profiles "
                "(id, agency_id, user_id, staff_code, status) "
                "VALUES (:id, :a, :u, :code, 'ACTIVE')"
            ),
            {"id": new_id, "a": agency_id, "u": user_id, "code": STAFF_CODE},
        )
        return new_id


async def _fetch_or_create_patient_profile(
    engine: AsyncEngine,
    *,
    agency_id: uuid.UUID,
    user_id: uuid.UUID,
) -> uuid.UUID:
    async with engine.begin() as conn:
        existing = (
            await conn.execute(
                text(
                    "SELECT id FROM patient_profiles "
                    "WHERE agency_id = :a AND user_id = :u"
                ),
                {"a": agency_id, "u": user_id},
            )
        ).first()
        if existing is not None:
            return cast("uuid.UUID", existing[0])
        new_id = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO patient_profiles "
                "(id, agency_id, user_id, patient_code, status, date_of_birth) "
                "VALUES (:id, :a, :u, :code, 'ACTIVE', '1990-01-01')"
            ),
            {"id": new_id, "a": agency_id, "u": user_id, "code": PATIENT_CODE},
        )
        return new_id


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

async def _seed() -> SeededIds:
    engine = create_async_engine(
        settings.effective_database_url,
        pool_pre_ping=True,
    )
    try:
        # Sanity check — bail loudly if Postgres is unreachable.
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:
            print(f"\nDatabase unreachable: {type(exc).__name__}: {exc}", file=sys.stderr)
            print(
                "Make sure Supabase is running (`supabase start`) or "
                "DATABASE_URL points at a reachable Postgres.",
                file=sys.stderr,
            )
            raise SystemExit(2) from exc

        agency_id = await _fetch_or_create_agency(engine)
        super_id = await _fetch_or_create_user(
            engine,
            email=SUPER_ADMIN_EMAIL,
            password=SUPER_ADMIN_PASSWORD,
            full_name=SUPER_ADMIN_NAME,
        )
        admin_id = await _fetch_or_create_user(
            engine,
            email=AGENCY_ADMIN_EMAIL,
            password=AGENCY_ADMIN_PASSWORD,
            full_name=AGENCY_ADMIN_NAME,
            phone="+1-555-0001",
        )
        staff_user_id = await _fetch_or_create_user(
            engine,
            email=STAFF_EMAIL,
            password=STAFF_PASSWORD,
            full_name=STAFF_NAME,
            phone="+1-555-0002",
        )
        patient_user_id = await _fetch_or_create_user(
            engine,
            email=PATIENT_EMAIL,
            password=PATIENT_PASSWORD,
            full_name=PATIENT_NAME,
            phone="+1-555-0003",
        )

        # Roles
        await _replace_role(engine, user_id=super_id, role="SUPER_ADMIN", agency_id=None)
        await _replace_role(engine, user_id=admin_id, role="AGENCY_ADMIN", agency_id=agency_id)
        await _replace_role(engine, user_id=staff_user_id, role="STAFF", agency_id=agency_id)
        await _replace_role(
            engine, user_id=patient_user_id, role="PATIENT", agency_id=agency_id
        )

        # Profiles (linked to the role users)
        staff_profile_id = await _fetch_or_create_staff_profile(
            engine, agency_id=agency_id, user_id=staff_user_id
        )
        patient_profile_id = await _fetch_or_create_patient_profile(
            engine, agency_id=agency_id, user_id=patient_user_id
        )

        return SeededIds(
            agency_id=agency_id,
            super_admin_id=super_id,
            agency_admin_id=admin_id,
            staff_user_id=staff_user_id,
            staff_profile_id=staff_profile_id,
            patient_user_id=patient_user_id,
            patient_profile_id=patient_profile_id,
        )
    finally:
        await engine.dispose()


def _write_env_test(ids: SeededIds) -> None:
    """Write the seeded IDs + credentials to `.env.test` so a developer
    can `source` it or open it in Postman directly."""
    path = Path(".env.test")
    lines = [
        "# Generated by scripts/seed_test_user.py — re-running rewrites this file.",
        f"QLOCKCARE_DEV_AGENCY_ID={ids.agency_id}",
        f"QLOCKCARE_DEV_SUPER_ADMIN_ID={ids.super_admin_id}",
        f"QLOCKCARE_DEV_ADMIN_ID={ids.agency_admin_id}",
        f"QLOCKCARE_DEV_STAFF_USER_ID={ids.staff_user_id}",
        f"QLOCKCARE_DEV_STAFF_PROFILE_ID={ids.staff_profile_id}",
        f"QLOCKCARE_DEV_PATIENT_USER_ID={ids.patient_user_id}",
        f"QLOCKCARE_DEV_PATIENT_PROFILE_ID={ids.patient_profile_id}",
        "",
        "# Credentials",
        f"QLOCKCARE_SUPER_ADMIN_EMAIL={SUPER_ADMIN_EMAIL}",
        f"QLOCKCARE_SUPER_ADMIN_PASSWORD={SUPER_ADMIN_PASSWORD}",
        f"QLOCKCARE_AGENCY_ADMIN_EMAIL={AGENCY_ADMIN_EMAIL}",
        f"QLOCKCARE_AGENCY_ADMIN_PASSWORD={AGENCY_ADMIN_PASSWORD}",
        f"QLOCKCARE_STAFF_EMAIL={STAFF_EMAIL}",
        f"QLOCKCARE_STAFF_PASSWORD={STAFF_PASSWORD}",
        f"QLOCKCARE_PATIENT_EMAIL={PATIENT_EMAIL}",
        f"QLOCKCARE_PATIENT_PASSWORD={PATIENT_PASSWORD}",
    ]
    path.write_text("\n".join(lines) + "\n")


def main() -> None:
    print("Seeding dev users...")
    ids = asyncio.run(_seed())
    print(f"  agency_id           = {ids.agency_id}")
    print(f"  super_admin_id      = {ids.super_admin_id}")
    print(f"  agency_admin_id     = {ids.agency_admin_id}")
    print(f"  staff_user_id       = {ids.staff_user_id}")
    print(f"  staff_profile_id    = {ids.staff_profile_id}")
    print(f"  patient_user_id     = {ids.patient_user_id}")
    print(f"  patient_profile_id  = {ids.patient_profile_id}")
    print()
    print("Logins (used by the Postman collection's `auth > Login` request):")
    print(f"  SUPER_ADMIN : {SUPER_ADMIN_EMAIL} / {SUPER_ADMIN_PASSWORD}")
    print(f"  AGENCY_ADMIN: {AGENCY_ADMIN_EMAIL} / {AGENCY_ADMIN_PASSWORD}")
    print(f"  STAFF       : {STAFF_EMAIL} / {STAFF_PASSWORD}")
    print(f"  PATIENT     : {PATIENT_EMAIL} / {PATIENT_PASSWORD}")
    print()
    # Skip writing `.env.test` if we're in CI (no use case for it there).
    if os.environ.get("CI") != "true":
        _write_env_test(ids)
        print("Wrote .env.test with the IDs above.")


if __name__ == "__main__":
    main()
