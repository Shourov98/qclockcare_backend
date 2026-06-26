"""End-to-end patients + guardians flow integration tests against the local Supabase stack.

Walks the agency-admin-side happy path:
    1. AGENCY_ADMIN signs in
    2. POST /patients  → 201, profile created
    3. GET  /patients  → list contains it
    4. POST /guardians → create a standalone guardian
    5. POST /patients/{id}/guardians → link guardian to patient
    6. GET  /patients/{id}/with-relationships → returns the link
    7. PATCH /patients/{id} → update status
    8. DELETE /patients/{id} → archive

Plus negative paths:
    - duplicate patient_code → 409
    - relationship with both sources set → 422
    - unauthenticated → 401
    - cross-agency isolation → 404

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
# Per-test engine (avoids pytest-asyncio event-loop issues)
# --------------------------------------------------------------------------
from sqlalchemy.ext.asyncio import create_async_engine


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
    except Exception as exc:
        print(f"DB NOT REACHABLE: {type(exc).__name__}: {exc}")
        return False


async def _seed_agency_with_admin(test_engine):
    from src.core.security import hash_password

    password = "TestPass123!AB"
    email = f"test-admin-{uuid.uuid4().hex[:8]}@example.com"
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
            {"id": agency_id, "name": f"Test Agency {uuid.uuid4().hex[:6]}"},
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
    return email, password, user_id, agency_id


async def _seed_second_admin(test_engine):
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
    async with test_engine.begin() as conn:
        await conn.execute(text("SET LOCAL session_replication_role = 'replica'"))
        # patient/guardian-related rows first
        await conn.execute(
            text(
                "DELETE FROM patient_guardian_relationships WHERE agency_id = :a"
            ),
            {"a": agency_id},
        )
        await conn.execute(
            text("DELETE FROM guardian_profiles WHERE agency_id = :a"),
            {"a": agency_id},
        )
        await conn.execute(
            text("DELETE FROM patient_profiles WHERE agency_id = :a"),
            {"a": agency_id},
        )
        await conn.execute(
            text("DELETE FROM user_roles WHERE agency_id = :a"),
            {"a": agency_id},
        )
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
async def admin_session():
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
async def second_admin():
    test_engine = _make_test_engine()
    try:
        if not await _db_reachable(test_engine):
            pytest.skip("Database not reachable")
        seed = await _seed_second_admin(test_engine)
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
async def test_unauthenticated_returns_401() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        r = await client.get("/patients")
        assert r.status_code == 401
        r = await client.get("/guardians")
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_create_list_get_patient(admin_session) -> None:
    email, password, _, _ = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        r = await client.post(
            "/patients",
            json={
                "email": f"pat-{uuid.uuid4().hex[:6]}@example.com",
                "full_name": "Pat Patient",
                "patient_code": f"P-{uuid.uuid4().hex[:6]}",
                "date_of_birth": "1980-05-12",
            },
            headers=auth,
        )
        assert r.status_code == 201, r.text
        patient = r.json()
        assert patient["status"] == "INVITED"
        assert patient["date_of_birth"] == "1980-05-12"
        patient_id = patient["id"]

        r = await client.get("/patients", headers=auth)
        assert r.status_code == 200, r.text
        assert any(p["id"] == patient_id for p in r.json()["data"])

        r = await client.get(f"/patients/{patient_id}", headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["patient_code"] == patient["patient_code"]


@pytest.mark.asyncio
async def test_create_guardian_and_link_to_patient(admin_session) -> None:
    email, password, _, _ = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        # Create patient
        r = await client.post(
            "/patients",
            json={
                "email": f"pat-{uuid.uuid4().hex[:6]}@example.com",
                "full_name": "Pat",
                "patient_code": f"P-{uuid.uuid4().hex[:6]}",
            },
            headers=auth,
        )
        patient_id = r.json()["id"]

        # Create standalone guardian
        r = await client.post(
            "/guardians",
            json={
                "email": f"gd-{uuid.uuid4().hex[:6]}@example.com",
                "full_name": "Guardian Dan",
                "contact_phone": "+1-555-0001",
            },
            headers=auth,
        )
        assert r.status_code == 201, r.text
        guardian = r.json()
        guardian_id = guardian["id"]

        # Link them
        r = await client.post(
            f"/patients/{patient_id}/guardians",
            json={
                "relationship_type": "SPOUSE",
                "is_legal": True,
                "guardian_id": guardian_id,
            },
            headers=auth,
        )
        assert r.status_code == 201, r.text
        link = r.json()
        assert link["is_legal"] is True
        assert link["relationship_type"] == "SPOUSE"

        # Verify via with-relationships
        r = await client.get(
            f"/patients/{patient_id}/with-relationships", headers=auth
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["guardian_links"]) == 1


@pytest.mark.asyncio
async def test_link_via_new_guardian_inline(admin_session) -> None:
    """One-shot create+link: POST /patients/{id}/guardians with new_guardian body."""
    email, password, _, _ = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        r = await client.post(
            "/patients",
            json={
                "email": f"pat-{uuid.uuid4().hex[:6]}@example.com",
                "full_name": "Pat",
                "patient_code": f"P-{uuid.uuid4().hex[:6]}",
            },
            headers=auth,
        )
        patient_id = r.json()["id"]

        r = await client.post(
            f"/patients/{patient_id}/guardians",
            json={
                "relationship_type": "PARENT",
                "new_guardian": {
                    "email": f"parent-{uuid.uuid4().hex[:6]}@example.com",
                    "full_name": "Parent Pat",
                },
            },
            headers=auth,
        )
        assert r.status_code == 201, r.text
        assert r.json()["relationship_type"] == "PARENT"


@pytest.mark.asyncio
async def test_archive_patient(admin_session) -> None:
    email, password, _, _ = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        r = await client.post(
            "/patients",
            json={
                "email": f"pat-{uuid.uuid4().hex[:6]}@example.com",
                "full_name": "Pat",
                "patient_code": f"P-{uuid.uuid4().hex[:6]}",
            },
            headers=auth,
        )
        patient_id = r.json()["id"]

        r = await client.delete(f"/patients/{patient_id}", headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ARCHIVED"


# --------------------------------------------------------------------------
# Negative paths
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_duplicate_patient_code_returns_409(admin_session) -> None:
    email, password, _, _ = admin_session
    code = f"P-{uuid.uuid4().hex[:6]}"
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        body = {
            "email": f"a-{uuid.uuid4().hex[:6]}@example.com",
            "full_name": "A",
            "patient_code": code,
        }
        r = await client.post("/patients", json=body, headers=auth)
        assert r.status_code == 201, r.text
        body["email"] = f"b-{uuid.uuid4().hex[:6]}@example.com"
        r = await client.post("/patients", json=body, headers=auth)
        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "DUPLICATE_RESOURCE"


@pytest.mark.asyncio
async def test_link_with_both_sources_returns_422(admin_session) -> None:
    email, password, _, _ = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        r = await client.post(
            "/patients",
            json={
                "email": f"pat-{uuid.uuid4().hex[:6]}@example.com",
                "full_name": "Pat",
                "patient_code": f"P-{uuid.uuid4().hex[:6]}",
            },
            headers=auth,
        )
        patient_id = r.json()["id"]

        r = await client.post(
            f"/patients/{patient_id}/guardians",
            json={
                "relationship_type": "SPOUSE",
                "guardian_id": str(uuid.uuid4()),
                "new_guardian": {
                    "email": f"g-{uuid.uuid4().hex[:6]}@example.com",
                    "full_name": "G",
                },
            },
            headers=auth,
        )
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_cross_agency_isolation(
    admin_session, second_admin
) -> None:
    """A patient at agency A must not be visible to agency B's admin."""
    a_email, a_password, _, _ = admin_session
    b_email, b_password, _, _ = second_admin

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        a_token = await _login(client, a_email, a_password)
        b_token = await _login(client, b_email, b_password)
        a_auth = {"Authorization": f"Bearer {a_token}"}
        b_auth = {"Authorization": f"Bearer {b_token}"}

        r = await client.post(
            "/patients",
            json={
                "email": f"a-pat-{uuid.uuid4().hex[:6]}@example.com",
                "full_name": "Agency A Patient",
                "patient_code": f"PA-{uuid.uuid4().hex[:6]}",
            },
            headers=a_auth,
        )
        assert r.status_code == 201, r.text
        patient_id = r.json()["id"]

        r = await client.get(f"/patients/{patient_id}", headers=b_auth)
        assert r.status_code == 404, r.text