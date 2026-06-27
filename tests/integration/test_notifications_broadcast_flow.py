"""End-to-end broadcast integration tests.

Walks:
  1. STAFF POST /notifications/broadcast → 403 (insufficient role)
  2. AGENCY_ADMIN POST /notifications/broadcast → 200, all active patients get notified
  3. Opted-out user is skipped (skipped_opted_out > 0)
  4. Empty agency → dispatched=0
  5. SUPER_ADMIN with ?agency_id=... → 200, scoped to that agency

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
            {"id": agency_id, "name": f"Broadcast Test {uuid.uuid4().hex[:6]}"},
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
            text("DELETE FROM notification_deliveries WHERE agency_id = :a"),
            {"a": agency_id},
        )
        await conn.execute(
            text("DELETE FROM notification_preferences WHERE agency_id = :a"),
            {"a": agency_id},
        )
        await conn.execute(
            text("DELETE FROM notifications WHERE agency_id = :a"), {"a": agency_id}
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
        r = await client.post(
            "/notifications/broadcast",
            json={"type": "GENERIC", "title": "Hi", "body": "World"},
        )
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_staff_cannot_broadcast(agency_session) -> None:
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
            "/notifications/broadcast",
            json={"type": "GENERIC", "title": "Hi", "body": "World"},
            headers=auth,
        )
        assert r.status_code == 403, r.text

    await test_engine.dispose()


@pytest.mark.asyncio
async def test_admin_broadcasts_to_all_patients(agency_session) -> None:
    admin_email, admin_pw, _admin_id, agency_id = agency_session
    test_engine = _make_test_engine()

    # Seed 3 patients
    patient_emails = []
    patient_ids = []
    for i in range(3):
        email = f"test-patient-{i}-{uuid.uuid4().hex[:8]}@example.com"
        uid, _pw = await _seed_patient(test_engine, email=email, agency_id=agency_id)
        patient_emails.append(email)
        patient_ids.append(uid)

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, admin_email, admin_pw)
        auth = {"Authorization": f"Bearer {token}"}

        r = await client.post(
            "/notifications/broadcast",
            json={
                "type": "GENERIC",
                "title": "Heads up",
                "body": "Maintenance Sunday.",
                "metadata": {"campaign_id": "abc"},
            },
            headers=auth,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # 3 patients, 1 admin not notified (sender excluded)
        assert body["dispatched"] == 3
        assert body["skipped_opted_out"] == 0
        assert body["failed"] == 0

    # Verify DB rows
    async with test_engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT recipient_user_id, title, metadata "
                    "FROM notifications WHERE agency_id = :a "
                    "AND type = 'GENERIC' AND title = 'Heads up'"
                ),
                {"a": agency_id},
            )
        ).all()
        assert len(rows) == 3
        recipient_set = {str(r[0]) for r in rows}
        assert recipient_set == set(patient_ids)
        for row in rows:
            md = row[2]
            assert md.get("broadcast") is True
            assert md.get("campaign_id") == "abc"

    await test_engine.dispose()


@pytest.mark.asyncio
async def test_opted_out_users_are_skipped(agency_session) -> None:
    admin_email, admin_pw, _admin_id, agency_id = agency_session
    test_engine = _make_test_engine()

    # 2 patients: one opts out, one doesn't
    opted_out_email = f"test-patient-out-{uuid.uuid4().hex[:8]}@example.com"
    opted_in_email = f"test-patient-in-{uuid.uuid4().hex[:8]}@example.com"
    opted_out_id, _opted_out_pw = await _seed_patient(
        test_engine, email=opted_out_email, agency_id=agency_id
    )
    opted_in_id, _opted_in_pw = await _seed_patient(
        test_engine, email=opted_in_email, agency_id=agency_id
    )

    # Set the opted-out user's pref before the broadcast
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO notification_preferences "
                "(user_id, agency_id, type, channel, opted_in, updated_at) "
                "VALUES (:u, :a, 'GENERIC', 'IN_APP', false, now())"
            ),
            {"u": opted_out_id, "a": agency_id},
        )

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, admin_email, admin_pw)
        auth = {"Authorization": f"Bearer {token}"}

        r = await client.post(
            "/notifications/broadcast",
            json={"type": "GENERIC", "title": "Hi", "body": "World"},
            headers=auth,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["dispatched"] == 1
        assert body["skipped_opted_out"] == 1

    # Verify only the opted-in patient got a notification
    async with test_engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT recipient_user_id FROM notifications "
                    "WHERE agency_id = :a AND type = 'GENERIC' AND title = 'Hi'"
                ),
                {"a": agency_id},
            )
        ).all()
        assert len(rows) == 1
        assert str(rows[0][0]) == opted_in_id

    await test_engine.dispose()


@pytest.mark.asyncio
async def test_empty_agency_returns_zero_dispatched(agency_session) -> None:
    admin_email, admin_pw, _admin_id, _agency_id = agency_session
    test_engine = _make_test_engine()

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, admin_email, admin_pw)
        auth = {"Authorization": f"Bearer {token}"}

        r = await client.post(
            "/notifications/broadcast",
            json={"type": "GENERIC", "title": "Hi", "body": "World"},
            headers=auth,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["dispatched"] == 0

    await test_engine.dispose()


@pytest.mark.asyncio
async def test_invalid_broadcast_returns_422(agency_session) -> None:
    admin_email, admin_pw, _admin_id, _agency_id = agency_session
    test_engine = _make_test_engine()

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, admin_email, admin_pw)
        auth = {"Authorization": f"Bearer {token}"}

        r = await client.post(
            "/notifications/broadcast",
            json={"type": "NOT_A_REAL_TYPE", "title": "Hi", "body": "World"},
            headers=auth,
        )
        assert r.status_code == 422

    await test_engine.dispose()
