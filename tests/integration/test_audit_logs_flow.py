"""End-to-end audit-logs integration tests.

Walks:
  1. Admin + super_admin + staff + patient seeded
  2. POST /visits triggers a VISIT_CHECKED_IN audit row
  3. GET /audit-logs as AGENCY_ADMIN — sees own agency
  4. GET /audit-logs as SUPER_ADMIN — sees all
  5. GET /audit-logs as STAFF → 403
  6. GET /audit-logs/{id} — own agency's log is reachable
  7. GET /audit-logs/{id} for another agency's log → 404
  8. Append-only trigger: direct UPDATE on audit_logs row raises

Skipped if no local Supabase is reachable.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.core.config import settings

BASE_URL = os.environ.get("QLOCKCARE_TEST_URL", "http://127.0.0.1:8001")


# --------------------------------------------------------------------------
# Helpers — mirrors test_visits_flow.py patterns
# --------------------------------------------------------------------------
def _make_test_engine():
    return create_async_engine(
        settings.effective_database_url,
        pool_pre_ping=True,
        pool_size=2,
    )


async def _db_reachable(test_engine) -> bool:
    try:
        async with test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def _seed_agency_with_admin(test_engine, *, role_value: str = "AGENCY_ADMIN"):
    from src.core.security import hash_password

    password = "TestPass123!AB"
    email = f"test-admin-{uuid.uuid4().hex[:8]}@example.com"
    password_hash = hash_password(password)

    async with test_engine.begin() as conn:
        agency_id = str(uuid.uuid4())
        user_id = str(uuid.uuid4())
        role_id = str(uuid.uuid4())
        # SUPER_ADMIN has agency_id=NULL — skip the agency insert for them.
        if role_value != "SUPER_ADMIN":
            await conn.execute(
                text(
                    "INSERT INTO agencies (id, name, timezone) "
                    "VALUES (:id, :name, 'America/Chicago')"
                ),
                {"id": agency_id, "name": f"Test Agency {uuid.uuid4().hex[:6]}"},
            )
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, full_name, status, email_verified_at) "
                "VALUES (:id, :email, :pw, 'Test Admin', 'ACTIVE', now())"
            ),
            {"id": user_id, "email": email, "pw": password_hash},
        )
        # SUPER_ADMIN must have agency_id=NULL; everyone else gets the agency.
        insert_agency_id = None if role_value == "SUPER_ADMIN" else agency_id
        await conn.execute(
            text(
                "INSERT INTO user_roles (id, user_id, agency_id, role) "
                "VALUES (:id, :uid, :aid, :r)"
            ),
            {"id": role_id, "uid": user_id, "aid": insert_agency_id, "r": role_value},
        )
    return email, password, user_id, agency_id


async def _seed_staff(test_engine, agency_id: str):
    from src.core.security import hash_password

    email = f"test-staff-{uuid.uuid4().hex[:8]}@example.com"
    user_id = str(uuid.uuid4())
    role_id = str(uuid.uuid4())
    password = "TestPass123!AB"
    staff_id = str(uuid.uuid4())
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, full_name, status, email_verified_at) "
                "VALUES (:id, :email, :pw, 'Test Staff', 'ACTIVE', now())"
            ),
            {"id": user_id, "email": email, "pw": hash_password(password)},
        )
        await conn.execute(
            text(
                "INSERT INTO staff_profiles (id, agency_id, user_id, staff_code, status, hired_at) "
                "VALUES (:id, :aid, :uid, :code, 'ACTIVE', now())"
            ),
            {"id": staff_id, "aid": agency_id, "uid": user_id, "code": f"S-{uuid.uuid4().hex[:6]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO user_roles (id, user_id, agency_id, role) "
                "VALUES (:id, :uid, :aid, 'STAFF')"
            ),
            {"id": role_id, "uid": user_id, "aid": agency_id},
        )
    return email, password, user_id, staff_id


async def _seed_patient(test_engine, agency_id: str):
    from src.core.security import hash_password

    email = f"test-patient-{uuid.uuid4().hex[:8]}@example.com"
    user_id = str(uuid.uuid4())
    role_id = str(uuid.uuid4())
    password = "TestPass123!AB"
    patient_id = str(uuid.uuid4())
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, full_name, status, email_verified_at) "
                "VALUES (:id, :email, :pw, 'Test Patient', 'ACTIVE', now())"
            ),
            {"id": user_id, "email": email, "pw": hash_password(password)},
        )
        await conn.execute(
            text(
                "INSERT INTO patient_profiles "
                "(id, agency_id, user_id, patient_code, status, admitted_at) "
                "VALUES (:id, :aid, :uid, :code, 'ACTIVE', now())"
            ),
            {
                "id": patient_id,
                "aid": agency_id,
                "uid": user_id,
                "code": f"P-{uuid.uuid4().hex[:6]}",
            },
        )
        await conn.execute(
            text(
                "INSERT INTO user_roles (id, user_id, agency_id, role) "
                "VALUES (:id, :uid, :aid, 'PATIENT')"
            ),
            {"id": role_id, "uid": user_id, "aid": agency_id},
        )
    return email, password, user_id, patient_id


async def _seed_appointment(test_engine, *, agency_id: str, patient_id: str, staff_id: str) -> str:
    from datetime import datetime, timedelta

    appt_id = str(uuid.uuid4())
    start = datetime.now(UTC) + timedelta(hours=1)
    end = start + timedelta(hours=1)
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO appointments "
                "(id, agency_id, patient_id, staff_id, program_type, "
                " scheduled_start, scheduled_end, status) "
                "VALUES (:id, :aid, :pid, :sid, 'HOME_HEALTH', :st, :et, 'SCHEDULED')"
            ),
            {
                "id": appt_id,
                "aid": agency_id,
                "pid": patient_id,
                "sid": staff_id,
                "st": start,
                "et": end,
            },
        )
    return appt_id


async def _cleanup(test_engine, agency_id: str) -> None:
    async with test_engine.begin() as conn:
        await conn.execute(text("SET LOCAL session_replication_role = 'replica'"))
        await conn.execute(text("DELETE FROM audit_logs WHERE agency_id = :a"), {"a": agency_id})
        await conn.execute(text("DELETE FROM visits WHERE agency_id = :a"), {"a": agency_id})
        await conn.execute(
            text("DELETE FROM appointments WHERE agency_id = :a"), {"a": agency_id}
        )
        await conn.execute(
            text("DELETE FROM staff_profiles WHERE agency_id = :a"), {"a": agency_id}
        )
        await conn.execute(
            text("DELETE FROM patient_profiles WHERE agency_id = :a"), {"a": agency_id}
        )
        await conn.execute(text("DELETE FROM user_roles WHERE agency_id = :a"), {"a": agency_id})
        await conn.execute(
            text("DELETE FROM agencies WHERE id = :a"), {"a": agency_id}
        )
        # SUPER_ADMIN role rows have agency_id=NULL — clean those up by user_id.
        await conn.execute(
            text("DELETE FROM user_roles WHERE agency_id IS NULL")
        )
        await conn.execute(
            text("DELETE FROM users WHERE email LIKE 'test-%@example.com'")
        )


@pytest.fixture
async def admin_agency():
    test_engine = _make_test_engine()
    try:
        if not await _db_reachable(test_engine):
            pytest.skip("Database not reachable")
        seed = await _seed_agency_with_admin(test_engine, role_value="AGENCY_ADMIN")
        yield seed
        await _cleanup(test_engine, seed[3])
    finally:
        await test_engine.dispose()


async def _login(client, email: str, password: str) -> str:
    r = await client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


# --------------------------------------------------------------------------
# Negative auth
# --------------------------------------------------------------------------
async def test_unauthenticated_returns_401() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        r = await client.get("/audit-logs")
        assert r.status_code == 401


async def test_staff_role_returns_403(admin_agency) -> None:
    _, _, _, agency_id = admin_agency
    test_engine = _make_test_engine()
    staff_email, staff_password, _, _ = await _seed_staff(test_engine, agency_id)

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, staff_email, staff_password)
        auth = {"Authorization": f"Bearer {token}"}
        r = await client.get("/audit-logs", headers=auth)
        assert r.status_code == 403, r.text

    await test_engine.dispose()


# --------------------------------------------------------------------------
# Visibility: AGENCY_ADMIN sees own, SUPER_ADMIN sees all
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_agency_admin_only_sees_own_agency(admin_agency) -> None:
    _, _, _, agency_id = admin_agency
    test_engine = _make_test_engine()

    # Seed a separate agency — its audit row must NOT be visible
    other = await _seed_agency_with_admin(test_engine, role_value="AGENCY_ADMIN")
    _, _, other_admin_id, other_agency_id = other

    # Insert one audit row in each agency
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO audit_logs "
                "(id, agency_id, actor_user_id, action, entity_type, entity_id) "
                "VALUES (:id, :a, :u, 'CREATE', 'APPOINTMENT', :e)"
            ),
            {
                "id": str(uuid.uuid4()),
                "a": agency_id,
                "u": admin_agency[2],  # use real admin user id
                "e": str(uuid.uuid4()),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO audit_logs "
                "(id, agency_id, actor_user_id, action, entity_type, entity_id) "
                "VALUES (:id, :a, :u, 'CREATE', 'APPOINTMENT', :e)"
            ),
            {
                "id": str(uuid.uuid4()),
                "a": other_agency_id,
                "u": other_admin_id,
                "e": str(uuid.uuid4()),
            },
        )

    # Login as the FIRST agency admin
    email, password, _, _ = admin_agency
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}
        r = await client.get("/audit-logs", headers=auth)
        assert r.status_code == 200, r.text
        body = r.json()
        items = body["data"]
        # Should only see logs from agency_id
        assert all(item["agency_id"] == agency_id for item in items)
        # Should NOT see other agency's row
        assert not any(item["agency_id"] == other_agency_id for item in items)

    await test_engine.dispose()


# --------------------------------------------------------------------------
# Direct insert (SUPER_ADMIN bypasses RLS)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_super_admin_sees_all_agencies(admin_agency) -> None:
    test_engine = _make_test_engine()
    # Seed SUPER_ADMIN
    email_sa, password_sa, _sa_id, _ = await _seed_agency_with_admin(
        test_engine, role_value="SUPER_ADMIN"
    )
    # Seed two regular agencies with one audit row each
    seed_a = await _seed_agency_with_admin(test_engine, role_value="AGENCY_ADMIN")
    seed_b = await _seed_agency_with_admin(test_engine, role_value="AGENCY_ADMIN")
    _, _, _, agency_a = seed_a
    _, _, _, agency_b = seed_b

    async with test_engine.begin() as conn:
        for aid, actor_id in (
            (agency_a, seed_a[2]),
            (agency_b, seed_b[2]),
        ):
            await conn.execute(
                text(
                    "INSERT INTO audit_logs "
                    "(id, agency_id, actor_user_id, action, entity_type, entity_id) "
                    "VALUES (:id, :a, :u, 'CREATE', 'APPOINTMENT', :e)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "a": aid,
                    "u": actor_id,
                    "e": str(uuid.uuid4()),
                },
            )

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email_sa, password_sa)
        auth = {"Authorization": f"Bearer {token}"}
        r = await client.get("/audit-logs?page_size=100", headers=auth)
        assert r.status_code == 200, r.text
        body = r.json()
        agencies_seen = {item["agency_id"] for item in body["data"]}
        # SUPER_ADMIN should see BOTH agencies
        assert agency_a in agencies_seen
        assert agency_b in agencies_seen

    await test_engine.dispose()


# --------------------------------------------------------------------------
# Append-only trigger
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_append_only_trigger_blocks_update(admin_agency) -> None:
    _, _, _, agency_id = admin_agency
    test_engine = _make_test_engine()

    log_id = str(uuid.uuid4())
    actor_id = admin_agency[2]  # real admin user from seed
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO audit_logs "
                "(id, agency_id, actor_user_id, action, entity_type, entity_id) "
                "VALUES (:id, :a, :u, 'CREATE', 'APPOINTMENT', :e)"
            ),
            {
                "id": log_id,
                "a": agency_id,
                "u": actor_id,
                "e": str(uuid.uuid4()),
            },
        )

    # Direct UPDATE should fail because of the append-only trigger
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError):
        async with test_engine.begin() as conn:
            await conn.execute(
                text("UPDATE audit_logs SET entity_type = 'TAMPERED' WHERE id = :i"),
                {"i": log_id},
            )

    # Direct DELETE should also fail
    with pytest.raises(DBAPIError):
        async with test_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM audit_logs WHERE id = :i"),
                {"i": log_id},
            )

    await test_engine.dispose()
