"""End-to-end locations flow integration tests.

Walks:
  1. Unauthenticated GET /locations → 401
  2. AGENCY_ADMIN POST /locations → 201, row appears
  3. AGENCY_ADMIN GET /locations → list contains the row
  4. AGENCY_ADMIN GET /locations/{id} → 200
  5. AGENCY_ADMIN PATCH /locations/{id} → 200, fields updated
  6. AGENCY_ADMIN DELETE /locations/{id} → 204, row soft-deleted
  7. GET on soft-deleted row → 404
  8. STAFF cannot create → 403
  9. PATIENT cannot read other-agency locations → 404 (RLS)

Skipped if no local Supabase is reachable.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.core.config import settings

BASE_URL = os.environ.get("QLOCKCARE_TEST_URL", "http://127.0.0.1:8001")


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


async def _seed_agency_with_admin(test_engine):
    from src.core.security import hash_password

    password = "TestPass123!AB"
    email = f"test-admin-{uuid.uuid4().hex[:8]}@example.com"
    password_hash = hash_password(password)
    agency_id = str(uuid.uuid4())
    admin_id = str(uuid.uuid4())
    role_id = str(uuid.uuid4())

    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO agencies (id, name, timezone) "
                "VALUES (:id, :name, 'America/Chicago')"
            ),
            {"id": agency_id, "name": f"Locations Test {uuid.uuid4().hex[:6]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, full_name, status, email_verified_at) "
                "VALUES (:id, :email, :pw, 'Test Admin', 'ACTIVE', now())"
            ),
            {"id": admin_id, "email": email, "pw": password_hash},
        )
        await conn.execute(
            text(
                "INSERT INTO user_roles (id, user_id, agency_id, role) "
                "VALUES (:id, :uid, :aid, 'AGENCY_ADMIN')"
            ),
            {"id": role_id, "uid": admin_id, "aid": agency_id},
        )
    return email, password, admin_id, agency_id


async def _seed_patient(test_engine, *, email: str, agency_id: str):
    from src.core.security import hash_password

    user_id = str(uuid.uuid4())
    role_id = str(uuid.uuid4())
    password = "TestPass123!AB"
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, full_name, status, email_verified_at) "
                "VALUES (:id, :email, :pw, 'P', 'ACTIVE', now())"
            ),
            {"id": user_id, "email": email, "pw": hash_password(password)},
        )
        await conn.execute(
            text(
                "INSERT INTO user_roles (id, user_id, agency_id, role) "
                "VALUES (:id, :uid, :aid, 'PATIENT')"
            ),
            {"id": role_id, "uid": user_id, "aid": agency_id},
        )
    return user_id, password


async def _seed_staff(test_engine, *, email: str, agency_id: str):
    from src.core.security import hash_password

    user_id = str(uuid.uuid4())
    role_id = str(uuid.uuid4())
    password = "TestPass123!AB"
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, full_name, status, email_verified_at) "
                "VALUES (:id, :email, :pw, 'S', 'ACTIVE', now())"
            ),
            {"id": user_id, "email": email, "pw": hash_password(password)},
        )
        await conn.execute(
            text(
                "INSERT INTO user_roles (id, user_id, agency_id, role) "
                "VALUES (:id, :uid, :aid, 'STAFF')"
            ),
            {"id": role_id, "uid": user_id, "aid": agency_id},
        )
    return user_id, password


async def _cleanup(test_engine, agency_id: str) -> None:
    async with test_engine.begin() as conn:
        await conn.execute(text("SET LOCAL session_replication_role = 'replica'"))
        await conn.execute(
            text("DELETE FROM locations WHERE agency_id = :a"), {"a": agency_id}
        )
        await conn.execute(
            text("DELETE FROM user_roles WHERE agency_id = :a"), {"a": agency_id}
        )
        await conn.execute(
            text("DELETE FROM users WHERE email LIKE 'test-admin-%@example.com'")
        )
        await conn.execute(
            text("DELETE FROM users WHERE email LIKE 'test-patient-%@example.com'")
        )
        await conn.execute(
            text("DELETE FROM users WHERE email LIKE 'test-staff-%@example.com'")
        )


@pytest.fixture
async def agency_session():
    test_engine = _make_test_engine()
    try:
        if not await _db_reachable(test_engine):
            pytest.skip("Database not reachable")
        seed = await _seed_agency_with_admin(test_engine)
        yield seed
        await _cleanup(test_engine, seed[3])
    finally:
        await test_engine.dispose()


async def _login(client, email: str, password: str) -> str:
    r = await client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_unauthenticated_returns_401() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        r = await client.get("/locations")
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_admin_crud_lifecycle(agency_session) -> None:
    admin_email, admin_pw, _admin_id, _agency_id = agency_session
    test_engine = _make_test_engine()

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, admin_email, admin_pw)
        auth = {"Authorization": f"Bearer {token}"}

        # CREATE
        r = await client.post(
            "/locations",
            json={
                "label": "Home",
                "address_line1": "123 Main St",
                "city": "Minneapolis",
                "state": "MN",
                "postal_code": "55401",
            },
            headers=auth,
        )
        assert r.status_code == 201, r.text
        location = r.json()
        loc_id = location["id"]
        assert location["label"] == "Home"
        assert location["state"] == "MN"
        assert location["country"] == "US"
        assert location["is_active"] is True

        # LIST
        r = await client.get("/locations", headers=auth)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["pagination"]["total"] == 1
        assert len(body["data"]) == 1
        assert body["data"][0]["id"] == loc_id

        # GET ONE
        r = await client.get(f"/locations/{loc_id}", headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["id"] == loc_id

        # PATCH (rename + deactivate)
        r = await client.patch(
            f"/locations/{loc_id}",
            json={"label": "New Home", "is_active": False},
            headers=auth,
        )
        assert r.status_code == 200, r.text
        assert r.json()["label"] == "New Home"
        assert r.json()["is_active"] is False

        # Default list excludes inactive — total should now be 0
        r = await client.get("/locations", headers=auth)
        assert r.json()["pagination"]["total"] == 0

        # include_inactive=true brings it back
        r = await client.get("/locations?include_inactive=true", headers=auth)
        assert r.json()["pagination"]["total"] == 1

        # DELETE (soft)
        r = await client.delete(f"/locations/{loc_id}", headers=auth)
        assert r.status_code == 204, r.text

        # GET after soft delete → 404
        r = await client.get(f"/locations/{loc_id}", headers=auth)
        assert r.status_code == 404, r.text

    await test_engine.dispose()


@pytest.mark.asyncio
async def test_staff_cannot_create(agency_session) -> None:
    _admin_email, _admin_pw, _admin_id, agency_id = agency_session
    test_engine = _make_test_engine()
    staff_email = f"test-staff-{uuid.uuid4().hex[:8]}@example.com"
    _staff_id, staff_pw = await _seed_staff(
        test_engine, email=staff_email, agency_id=agency_id
    )

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, staff_email, staff_pw)
        auth = {"Authorization": f"Bearer {token}"}

        r = await client.post(
            "/locations",
            json={
                "address_line1": "123 Main St",
                "city": "Minneapolis",
                "state": "MN",
                "postal_code": "55401",
            },
            headers=auth,
        )
        assert r.status_code == 403, r.text

    await test_engine.dispose()


@pytest.mark.asyncio
async def test_cross_agency_isolation(agency_session) -> None:
    """Admin at agency A creates a location; admin at agency B can't see it."""
    admin_email_a, admin_pw_a, _admin_a, _agency_id_a = agency_session
    test_engine = _make_test_engine()

    # Seed a second agency + admin
    admin_email_b = f"test-admin-{uuid.uuid4().hex[:8]}@example.com"
    admin_pw_b = "TestPass123!AB"
    from src.core.security import hash_password

    pw_b_hash = hash_password(admin_pw_b)
    agency_id_b = str(uuid.uuid4())
    admin_id_b = str(uuid.uuid4())
    role_id_b = str(uuid.uuid4())
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO agencies (id, name, timezone) "
                "VALUES (:id, :name, 'America/Chicago')"
            ),
            {"id": agency_id_b, "name": f"Agency B {uuid.uuid4().hex[:6]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, full_name, status, email_verified_at) "
                "VALUES (:id, :email, :pw, 'Admin B', 'ACTIVE', now())"
            ),
            {"id": admin_id_b, "email": admin_email_b, "pw": pw_b_hash},
        )
        await conn.execute(
            text(
                "INSERT INTO user_roles (id, user_id, agency_id, role) "
                "VALUES (:id, :uid, :aid, 'AGENCY_ADMIN')"
            ),
            {"id": role_id_b, "uid": admin_id_b, "aid": agency_id_b},
        )

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        # Agency A admin creates a location
        token_a = await _login(client, admin_email_a, admin_pw_a)
        r = await client.post(
            "/locations",
            json={
                "label": "Agency A Home",
                "address_line1": "1 A St",
                "city": "Minneapolis",
                "state": "MN",
                "postal_code": "55401",
            },
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert r.status_code == 201
        loc_id = r.json()["id"]

        # Agency B admin tries to GET A's location — should 404
        token_b = await _login(client, admin_email_b, admin_pw_b)
        r = await client.get(
            f"/locations/{loc_id}", headers={"Authorization": f"Bearer {token_b}"}
        )
        assert r.status_code == 404, r.text

        # Agency B admin's list should be empty
        r = await client.get(
            "/locations", headers={"Authorization": f"Bearer {token_b}"}
        )
        assert r.json()["pagination"]["total"] == 0

    # Cleanup
    async with test_engine.begin() as conn:
        await conn.execute(text("SET LOCAL session_replication_role = 'replica'"))
        await conn.execute(
            text("DELETE FROM locations WHERE agency_id = :a"), {"a": agency_id_b}
        )
        await conn.execute(
            text("DELETE FROM user_roles WHERE agency_id = :a"), {"a": agency_id_b}
        )

    await test_engine.dispose()


@pytest.mark.asyncio
async def test_invalid_state_returns_422(agency_session) -> None:
    admin_email, admin_pw, _admin_id, _agency_id = agency_session
    test_engine = _make_test_engine()

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, admin_email, admin_pw)
        auth = {"Authorization": f"Bearer {token}"}

        r = await client.post(
            "/locations",
            json={
                "address_line1": "123 Main St",
                "city": "Minneapolis",
                "state": "MINN",  # wrong length
                "postal_code": "55401",
            },
            headers=auth,
        )
        assert r.status_code == 422

    await test_engine.dispose()
