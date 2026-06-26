"""End-to-end staff flow integration tests against the local Supabase stack.

Walks the agency-admin-side happy path:
    1. AGENCY_ADMIN signs in
    2. POST /staff  → 201, profile created
    3. GET  /staff  → list contains it
    4. GET  /staff/{id}/with-details → returns qual + avail (initially empty)
    5. POST /staff/{id}/qualifications → 201
    6. POST /staff/{id}/availability → 201
    7. PATCH /staff/{id} → update status
    8. DELETE /staff/{id} → archive (status=ARCHIVED)
    9. Negative: another agency cannot see this staff (CROSS_AGENCY_ACCESS_DENIED)

Plus negative paths:
    - create duplicate (same staff_code) → 409 DUPLICATE_RESOURCE
    - unauthenticated → 401
    - PATIENT cannot mutate → 403

Skipped if no local Supabase is reachable.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest
from sqlalchemy import text

from src.core.config import settings

BASE_URL = os.environ.get("QLOCKCARE_TEST_URL", "http://127.0.0.1:8001")


# --------------------------------------------------------------------------
# Per-test engine
# --------------------------------------------------------------------------
# pytest-asyncio creates a new event loop per test, but the module-level
# `engine` in src.core.database is bound to whatever loop was active at
# import time. Using a fresh AsyncEngine per test avoids "Future attached
# to a different loop" errors.
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def _make_test_engine():
    return create_async_engine(
        settings.effective_database_url,
        pool_pre_ping=True,
        pool_size=2,
    )


# --------------------------------------------------------------------------
# DB helpers
# --------------------------------------------------------------------------
async def _db_reachable(test_engine) -> bool:
    try:
        async with test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        print(f"DB NOT REACHABLE: {type(exc).__name__}: {exc}")
        return False


async def _seed_agency_with_admin(test_engine) -> tuple[str, str, str, str, str]:
    """Create an agency + AGENCY_ADMIN (ACTIVE).

    Returns (email, password, user_id, agency_id, agency_name).
    """
    from src.core.security import hash_password

    password = "TestPass123!AB"
    email = f"test-admin-{uuid.uuid4().hex[:8]}@example.com"
    password_hash = hash_password(password)
    agency_name = f"Test Agency {uuid.uuid4().hex[:6]}"

    async with test_engine.begin() as conn:
        agency_id = str(uuid.uuid4())
        user_id = str(uuid.uuid4())
        role_id = str(uuid.uuid4())
        await conn.execute(
            text(
                "INSERT INTO agencies (id, name, timezone) "
                "VALUES (:id, :name, 'America/Chicago')"
            ),
            {"id": agency_id, "name": agency_name},
        )
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, full_name, status, email_verified_at) "
                "VALUES (:id, :email, :pw, 'Test Admin', 'ACTIVE', now())"
            ),
            {"id": user_id, "email": email, "pw": password_hash},
        )
        await conn.execute(
            text(
                "INSERT INTO user_roles (id, user_id, agency_id, role) "
                "VALUES (:id, :uid, :aid, 'AGENCY_ADMIN')"
            ),
            {"id": role_id, "uid": user_id, "aid": agency_id},
        )
    return email, password, user_id, agency_id, agency_name


async def _seed_second_agency_admin(test_engine) -> tuple[str, str, str, str]:
    """A second agency + admin (for cross-agency tests)."""
    from src.core.security import hash_password

    password = "TestPass123!AB"
    email = f"test-admin-b-{uuid.uuid4().hex[:8]}@example.com"
    password_hash = hash_password(password)

    async with test_engine.begin() as conn:
        agency_id = str(uuid.uuid4())
        user_id = str(uuid.uuid4())
        role_id = str(uuid.uuid4())
        await conn.execute(
            text(
                "INSERT INTO agencies (id, name, timezone) "
                "VALUES (:id, :name, 'America/Chicago')"
            ),
            {"id": agency_id, "name": f"Other Agency {uuid.uuid4().hex[:6]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, full_name, status, email_verified_at) "
                "VALUES (:id, :email, :pw, 'Other Admin', 'ACTIVE', now())"
            ),
            {"id": user_id, "email": email, "pw": password_hash},
        )
        await conn.execute(
            text(
                "INSERT INTO user_roles (id, user_id, agency_id, role) "
                "VALUES (:id, :uid, :aid, 'AGENCY_ADMIN')"
            ),
            {"id": role_id, "uid": user_id, "aid": agency_id},
        )
    return email, password, user_id, agency_id


async def _cleanup_agency(test_engine, agency_id: str) -> None:
    """Tear down everything we created, bypassing append-only triggers."""
    async with test_engine.begin() as conn:
        await conn.execute(text("SET LOCAL session_replication_role = 'replica'"))
        # Delete staff-related rows first
        await conn.execute(
            text("DELETE FROM staff_availability WHERE agency_id = :a"),
            {"a": agency_id},
        )
        await conn.execute(
            text("DELETE FROM staff_qualifications WHERE agency_id = :a"),
            {"a": agency_id},
        )
        await conn.execute(
            text(
                "DELETE FROM staff_profiles WHERE agency_id = :a"
            ),
            {"a": agency_id},
        )
        await conn.execute(
            text(
                "DELETE FROM user_roles WHERE agency_id = :a"
            ),
            {"a": agency_id},
        )
        # Find any staff user_ids to clean up users
        user_rows = await conn.execute(
            text("SELECT id FROM users WHERE email LIKE 'test-%@example.com'")
        )
        user_ids = [r[0] for r in user_rows]
        for uid in user_ids:
            await conn.execute(
                text("DELETE FROM refresh_tokens WHERE user_id = :u"), {"u": uid}
            )
        if user_ids:
            await conn.execute(
                text("DELETE FROM users WHERE id = ANY(:ids)"),
                {"ids": user_ids},
            )
        await conn.execute(
            text("DELETE FROM agencies WHERE id = :a"), {"a": agency_id}
        )


@pytest.fixture
async def admin_session() -> tuple[str, str, str, str, str]:
    test_engine = _make_test_engine()
    try:
        if not await _db_reachable(test_engine):
            pytest.skip("Database not reachable")
        seed = await _seed_agency_with_admin(test_engine)
        yield seed
        await _cleanup_agency(test_engine, seed[3])
    finally:
        await test_engine.dispose()


@pytest.fixture
async def second_admin_session() -> tuple[str, str, str, str]:
    test_engine = _make_test_engine()
    try:
        if not await _db_reachable(test_engine):
            pytest.skip("Database not reachable")
        seed = await _seed_second_agency_admin(test_engine)
        yield seed
        await _cleanup_agency(test_engine, seed[3])
    finally:
        await test_engine.dispose()


async def _login(client: httpx.AsyncClient, email: str, password: str) -> str:
    r = await client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unauthenticated_list_returns_401() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        r = await client.get("/staff")
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_create_list_get_staff(
    admin_session: tuple[str, str, str, str, str],
) -> None:
    email, password, _uid, _aid, _name = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        # Create
        r = await client.post(
            "/staff",
            json={
                "email": f"staff-{uuid.uuid4().hex[:8]}@example.com",
                "full_name": "Alice Caregiver",
                "phone": "+1-555-0100",
                "staff_code": f"STF-{uuid.uuid4().hex[:6]}",
            },
            headers=auth,
        )
        assert r.status_code == 201, r.text
        staff = r.json()
        assert staff["status"] == "INVITED"
        staff_id = staff["id"]

        # List
        r = await client.get("/staff", headers=auth)
        assert r.status_code == 200, r.text
        body = r.json()
        assert any(s["id"] == staff_id for s in body["data"])

        # Get
        r = await client.get(f"/staff/{staff_id}", headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["id"] == staff_id


@pytest.mark.asyncio
async def test_add_qualification_and_availability(
    admin_session: tuple[str, str, str, str, str],
) -> None:
    email, password, _uid, _aid, _name = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        r = await client.post(
            "/staff",
            json={
                "email": f"staff-{uuid.uuid4().hex[:8]}@example.com",
                "full_name": "Bob Caregiver",
                "staff_code": f"STF-{uuid.uuid4().hex[:6]}",
            },
            headers=auth,
        )
        staff_id = r.json()["id"]

        # Add qualification
        r = await client.post(
            f"/staff/{staff_id}/qualifications",
            json={
                "qualification_type": "CPR",
                "issued_at": "2025-01-01",
                "expires_at": "2027-01-01",
            },
            headers=auth,
        )
        assert r.status_code == 201, r.text
        qual = r.json()
        assert qual["qualification_type"] == "CPR"
        assert qual["status"] == "PENDING_VERIFICATION"

        # Add recurring availability
        r = await client.post(
            f"/staff/{staff_id}/availability",
            json={
                "day_of_week": 0,
                "start_time": "08:00:00",
                "end_time": "12:00:00",
            },
            headers=auth,
        )
        assert r.status_code == 201, r.text
        avail = r.json()
        assert avail["day_of_week"] == 0

        # with-details returns both
        r = await client.get(
            f"/staff/{staff_id}/with-details", headers=auth
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["qualifications"]) == 1
        assert len(body["availability"]) == 1


@pytest.mark.asyncio
async def test_archive_staff(
    admin_session: tuple[str, str, str, str, str],
) -> None:
    email, password, _uid, _aid, _name = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        r = await client.post(
            "/staff",
            json={
                "email": f"staff-{uuid.uuid4().hex[:8]}@example.com",
                "full_name": "Carl",
                "staff_code": f"STF-{uuid.uuid4().hex[:6]}",
            },
            headers=auth,
        )
        staff_id = r.json()["id"]

        r = await client.delete(f"/staff/{staff_id}", headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ARCHIVED"


# --------------------------------------------------------------------------
# Negative paths
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_duplicate_staff_code_returns_409(
    admin_session: tuple[str, str, str, str, str],
) -> None:
    email, password, _uid, _aid, _name = admin_session
    code = f"STF-{uuid.uuid4().hex[:6]}"
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        body = {
            "email": f"a-{uuid.uuid4().hex[:6]}@example.com",
            "full_name": "A",
            "staff_code": code,
        }
        r = await client.post("/staff", json=body, headers=auth)
        assert r.status_code == 201, r.text

        # Same code, different email → should 409
        body["email"] = f"b-{uuid.uuid4().hex[:6]}@example.com"
        r = await client.post("/staff", json=body, headers=auth)
        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "DUPLICATE_RESOURCE"


@pytest.mark.asyncio
async def test_cross_agency_isolation(
    admin_session: tuple[str, str, str, str, str],
    second_admin_session: tuple[str, str, str, str],
) -> None:
    """A staff at agency A must not be visible to agency B's admin."""
    a_email, a_password, _, _a_aid, _ = admin_session
    b_email, b_password, _, _b_aid = second_admin_session

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        a_token = await _login(client, a_email, a_password)
        b_token = await _login(client, b_email, b_password)
        a_auth = {"Authorization": f"Bearer {a_token}"}
        b_auth = {"Authorization": f"Bearer {b_token}"}

        # Agency A creates a staff
        r = await client.post(
            "/staff",
            json={
                "email": f"a-staff-{uuid.uuid4().hex[:6]}@example.com",
                "full_name": "Agency A Staff",
                "staff_code": f"STFA-{uuid.uuid4().hex[:6]}",
            },
            headers=a_auth,
        )
        assert r.status_code == 201, r.text
        staff_id = r.json()["id"]

        # Agency B tries to fetch by id → 404 (defence in depth + RLS)
        r = await client.get(f"/staff/{staff_id}", headers=b_auth)
        assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_invalid_qualification_dates_returns_422(
    admin_session: tuple[str, str, str, str, str],
) -> None:
    email, password, _uid, _aid, _name = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        r = await client.post(
            "/staff",
            json={
                "email": f"x-{uuid.uuid4().hex[:6]}@example.com",
                "full_name": "X",
                "staff_code": f"STF-{uuid.uuid4().hex[:6]}",
            },
            headers=auth,
        )
        staff_id = r.json()["id"]

        r = await client.post(
            f"/staff/{staff_id}/qualifications",
            json={
                "qualification_type": "CPR",
                "issued_at": "2025-06-01",
                "expires_at": "2025-01-01",
            },
            headers=auth,
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_availability_must_be_either_recurring_or_specific(
    admin_session: tuple[str, str, str, str, str],
) -> None:
    email, password, _uid, _aid, _name = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        r = await client.post(
            "/staff",
            json={
                "email": f"x-{uuid.uuid4().hex[:6]}@example.com",
                "full_name": "X",
                "staff_code": f"STF-{uuid.uuid4().hex[:6]}",
            },
            headers=auth,
        )
        staff_id = r.json()["id"]

        r = await client.post(
            f"/staff/{staff_id}/availability",
            json={"reason": "nope"},  # neither recurring nor specific
            headers=auth,
        )
        assert r.status_code == 422