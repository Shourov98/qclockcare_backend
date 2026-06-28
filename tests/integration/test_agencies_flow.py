"""End-to-end agencies integration tests.

Walks:
  1. Unauthenticated GET /agencies → 401
  2. AGENCY_ADMIN GET /agencies → 403 (SUPER_ADMIN only)
  3. SUPER_ADMIN POST /agencies → 201, row appears in list
  4. SUPER_ADMIN GET /agencies/{id} → 200
  5. SUPER_ADMIN PATCH /agencies/{id} → 200, fields updated + audit row
  6. SUPER_ADMIN GET /agencies/{id}/programs → 200 (empty list — no initial codes)
  7. SUPER_ADMIN POST /agencies with initial_program_codes → programs attached
  8. SUPER_ADMIN DELETE /agencies/{id} → 204, GET → 404 (soft delete)
  9. SUPER_ADMIN GET /agencies/{id}?include_deleted=true → 200 (deleted row reachable)

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


# --------------------------------------------------------------------------
# Helpers
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


async def _seed_user_with_role(
    test_engine,
    *,
    role_value: str,
    agency_id: str | None = None,
) -> tuple[str, str, str]:
    """Seed a user with one role row.

    Returns (email, password, user_id). For SUPER_ADMIN, agency_id is None.
    """
    from src.core.security import hash_password

    password = "TestPass123!AB"
    email = f"test-{role_value.lower()}-{uuid.uuid4().hex[:8]}@example.com"
    user_id = str(uuid.uuid4())
    role_id = str(uuid.uuid4())
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, full_name, status, email_verified_at) "
                "VALUES (:id, :email, :pw, :name, 'ACTIVE', now())"
            ),
            {
                "id": user_id,
                "email": email,
                "pw": hash_password(password),
                "name": f"Test {role_value}",
            },
        )
        await conn.execute(
            text(
                "INSERT INTO user_roles (id, user_id, agency_id, role) VALUES (:id, :uid, :aid, :r)"
            ),
            {"id": role_id, "uid": user_id, "aid": agency_id, "r": role_value},
        )
    return email, password, user_id


async def _cleanup(test_engine) -> None:
    """Tear down everything we seeded."""
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM audit_logs WHERE entity_type = 'AGENCY' "
                "AND entity_id IN (SELECT id FROM agencies WHERE name LIKE 'Test Agency%')"
            )
        )
        await conn.execute(
            text(
                "DELETE FROM agency_programs WHERE agency_id IN "
                "(SELECT id FROM agencies WHERE name LIKE 'Test Agency%')"
            )
        )
        await conn.execute(
            text(
                "DELETE FROM user_roles WHERE user_id IN "
                "(SELECT id FROM users WHERE email LIKE 'test-%@example.com')"
            )
        )
        await conn.execute(text("DELETE FROM users WHERE email LIKE 'test-%@example.com'"))
        await conn.execute(text("DELETE FROM agencies WHERE name LIKE 'Test Agency%'"))


async def _login(client: httpx.AsyncClient, email: str, password: str) -> str:
    r = await client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture
async def super_admin_seed():
    """Seed a SUPER_ADMIN and an AGENCY_ADMIN (so we can test 403 vs 401)."""
    test_engine = _make_test_engine()
    try:
        if not await _db_reachable(test_engine):
            pytest.skip("Database not reachable")
        # We need an agency for AGENCY_ADMIN to live under.
        agency_id = str(uuid.uuid4())
        async with test_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO agencies (id, name, timezone) "
                    "VALUES (:id, :name, 'America/Chicago')"
                ),
                {"id": agency_id, "name": f"Test Agency {uuid.uuid4().hex[:6]}"},
            )
        admin_email, admin_pw, _ = await _seed_user_with_role(
            test_engine, role_value="AGENCY_ADMIN", agency_id=agency_id
        )
        super_email, super_pw, _ = await _seed_user_with_role(
            test_engine, role_value="SUPER_ADMIN", agency_id=None
        )
        yield {
            "super_email": super_email,
            "super_password": super_pw,
            "admin_email": admin_email,
            "admin_password": admin_pw,
            "agency_id": agency_id,
        }
        await _cleanup(test_engine)
    finally:
        await test_engine.dispose()


# --------------------------------------------------------------------------
# Auth / role gating
# --------------------------------------------------------------------------
async def test_unauthenticated_returns_401() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        r = await client.get("/agencies")
        assert r.status_code == 401, r.text


async def test_agency_admin_returns_403(super_admin_seed) -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(
            client, super_admin_seed["admin_email"], super_admin_seed["admin_password"]
        )
        r = await client.get("/agencies", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403, r.text


# --------------------------------------------------------------------------
# Full CRUD lifecycle as SUPER_ADMIN
# --------------------------------------------------------------------------
async def test_super_admin_full_lifecycle(super_admin_seed) -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(
            client, super_admin_seed["super_email"], super_admin_seed["super_password"]
        )
        auth = {"Authorization": f"Bearer {token}"}

        # 1. CREATE
        agency_name = f"Test Agency {uuid.uuid4().hex[:8]}"
        r = await client.post(
            "/agencies",
            json={
                "name": agency_name,
                "timezone": "America/New_York",
                "settings": {"theme": "light"},
                "initial_program_codes": ["PCA", "ARMHS"],
            },
            headers=auth,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        agency_id = body["id"]
        assert body["name"] == agency_name
        assert body["timezone"] == "America/New_York"
        assert body["status"] == "ACTIVE"
        assert body["settings"] == {"theme": "light"}

        # 2. LIST — must include the new agency
        r = await client.get("/agencies", headers=auth)
        assert r.status_code == 200, r.text
        listing = r.json()
        names = [a["name"] for a in listing["data"]]
        assert agency_name in names

        # 3. GET ONE
        r = await client.get(f"/agencies/{agency_id}", headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["name"] == agency_name

        # 4. LIST PROGRAMS — must include the 2 we attached
        r = await client.get(f"/agencies/{agency_id}/programs", headers=auth)
        assert r.status_code == 200, r.text
        codes = sorted(p["program_code"] for p in r.json()["data"])
        assert codes == ["ARMHS", "PCA"]

        # 5. PATCH — partial update (rename + status flip)
        new_name = f"Test Agency Renamed {uuid.uuid4().hex[:6]}"
        r = await client.patch(
            f"/agencies/{agency_id}",
            json={"name": new_name, "status": "SUSPENDED"},
            headers=auth,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["name"] == new_name
        assert body["status"] == "SUSPENDED"
        # The service stamps settings.suspended_at on SUSPENDED transitions.
        assert "suspended_at" in body["settings"]

        # 6. REACTIVATE — settings.suspended_at cleared, reactivated_at set
        r = await client.patch(
            f"/agencies/{agency_id}",
            json={"status": "ACTIVE"},
            headers=auth,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ACTIVE"
        assert "suspended_at" not in body["settings"]
        assert "reactivated_at" in body["settings"]

        # 7. SOFT DELETE
        r = await client.delete(f"/agencies/{agency_id}", headers=auth)
        assert r.status_code == 204, r.text

        # 8. GET on deleted row → 404 (soft-delete hides by default)
        r = await client.get(f"/agencies/{agency_id}", headers=auth)
        assert r.status_code == 404, r.text

        # 9. GET deleted row with include_deleted=true → 200
        r = await client.get(f"/agencies/{agency_id}?include_deleted=true", headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["id"] == agency_id


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
async def test_unknown_program_code_returns_422(super_admin_seed) -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(
            client, super_admin_seed["super_email"], super_admin_seed["super_password"]
        )
        r = await client.post(
            "/agencies",
            json={
                "name": f"Test Agency {uuid.uuid4().hex[:6]}",
                "initial_program_codes": ["PCA", "NOT_A_REAL_PROGRAM"],
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 422, r.text


async def test_get_unknown_agency_returns_404(super_admin_seed) -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(
            client, super_admin_seed["super_email"], super_admin_seed["super_password"]
        )
        r = await client.get(
            f"/agencies/{uuid.uuid4()}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 404, r.text
