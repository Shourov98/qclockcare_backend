"""End-to-end unread badge integration tests.

Walks:
  1. Unauthenticated GET /notifications/badge → 401
  2. Patient baseline badge → 0
  3. After inserting 2 unread notifications, badge → 2
  4. After marking 1 read, badge → 1
  5. After mark-all-read, badge → 0

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
            {"id": agency_id, "name": f"Badge Test {uuid.uuid4().hex[:6]}"},
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


async def _insert_notification(test_engine, *, agency_id: str, recipient_user_id: str):
    nid = str(uuid.uuid4())
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO notifications "
                "(id, agency_id, recipient_user_id, type, title, body, status, metadata) "
                "VALUES (:id, :a, :u, 'GENERIC', 'Hi', 'World', 'SENT', '{}'::jsonb)"
            ),
            {"id": nid, "a": agency_id, "u": recipient_user_id},
        )
    return nid


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
        r = await client.get("/notifications/badge")
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_badge_baseline_is_zero(agency_session) -> None:
    _, _, _, agency_id = agency_session
    test_engine = _make_test_engine()
    user_email = f"test-patient-{uuid.uuid4().hex[:8]}@example.com"
    _user_id, password = await _seed_patient(
        test_engine, email=user_email, agency_id=agency_id
    )

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, user_email, password)
        auth = {"Authorization": f"Bearer {token}"}

        r = await client.get("/notifications/badge", headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["unread_count"] == 0

    await test_engine.dispose()


@pytest.mark.asyncio
async def test_badge_increments_and_decrements(agency_session) -> None:
    _, _, _, agency_id = agency_session
    test_engine = _make_test_engine()
    user_email = f"test-patient-{uuid.uuid4().hex[:8]}@example.com"
    user_id, password = await _seed_patient(
        test_engine, email=user_email, agency_id=agency_id
    )

    # Insert 2 unread notifications
    n1 = await _insert_notification(
        test_engine, agency_id=agency_id, recipient_user_id=user_id
    )
    await _insert_notification(test_engine, agency_id=agency_id, recipient_user_id=user_id)

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, user_email, password)
        auth = {"Authorization": f"Bearer {token}"}

        r = await client.get("/notifications/badge", headers=auth)
        assert r.json()["unread_count"] == 2

        # Mark one read
        r = await client.patch(f"/notifications/{n1}/read", headers=auth)
        assert r.status_code == 200

        r = await client.get("/notifications/badge", headers=auth)
        assert r.json()["unread_count"] == 1

        # Mark all read
        r = await client.post("/notifications/read-all", headers=auth)
        assert r.status_code == 200

        r = await client.get("/notifications/badge", headers=auth)
        assert r.json()["unread_count"] == 0

    await test_engine.dispose()
