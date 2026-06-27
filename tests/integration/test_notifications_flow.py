"""End-to-end notifications flow integration tests.

Walks:
  1. Admin + patient user seeded
  2. Patient attaches to a patient profile
  3. Manually insert a notification row
  4. Patient logs in, GET /notifications — sees it
  5. PATCH /notifications/{id}/read — marks read
  6. POST /notifications/read-all — marks all
  7. unread_count reflects changes
  8. Cross-user isolation — patient B can't see patient A's notifications
  9. GET /notifications/{id} for someone else's notif returns 404

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
            {"id": agency_id, "name": f"Test Agency {uuid.uuid4().hex[:6]}"},
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


async def _seed_user(test_engine, *, email: str, agency_id: str, full_name: str = "T"):
    from src.core.security import hash_password

    user_id = str(uuid.uuid4())
    role_id = str(uuid.uuid4())
    password = "TestPass123!AB"
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, full_name, status, email_verified_at) "
                "VALUES (:id, :email, :pw, :fn, 'ACTIVE', now())"
            ),
            {"id": user_id, "email": email, "pw": hash_password(password), "fn": full_name},
        )
        await conn.execute(
            text(
                "INSERT INTO user_roles (id, user_id, agency_id, role) "
                "VALUES (:id, :uid, :aid, 'PATIENT')"
            ),
            {"id": role_id, "uid": user_id, "aid": agency_id},
        )
    return user_id, password


async def _insert_notification(
    test_engine,
    *,
    agency_id: str,
    recipient_user_id: str,
    notif_type: str = "GENERIC",
    title: str = "Hello",
    body: str = "World",
    metadata: dict | None = None,
) -> str:
    notif_id = str(uuid.uuid4())
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO notifications "
                "(id, agency_id, recipient_user_id, type, title, body, status, metadata) "
                "VALUES (:id, :aid, :uid, :t, :title, :body, 'SENT', CAST(:md AS jsonb))"
            ),
            {
                "id": notif_id,
                "aid": agency_id,
                "uid": recipient_user_id,
                "t": notif_type,
                "title": title,
                "body": body,
                "md": "{}" if metadata is None else str(metadata).replace("'", '"'),
            },
        )
    return notif_id


async def _cleanup(test_engine, agency_id: str) -> None:
    async with test_engine.begin() as conn:
        await conn.execute(text("SET LOCAL session_replication_role = 'replica'"))
        await conn.execute(text("DELETE FROM notifications WHERE agency_id = :a"), {"a": agency_id})
        await conn.execute(text("DELETE FROM user_roles WHERE agency_id = :a"), {"a": agency_id})
        await conn.execute(text("DELETE FROM users WHERE email LIKE 'test-%@example.com'"))


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
    r = await client.post(
        "/auth/login", json={"email": email, "password": password}
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def test_unauthenticated_returns_401() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        r = await client.get("/notifications")
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_mark_read_mark_all(agency_session) -> None:
    _, _, _, agency_id = agency_session
    test_engine = _make_test_engine()

    # Two patient users, each gets 2 notifications
    user_a_email = f"test-patient-a-{uuid.uuid4().hex[:8]}@example.com"
    user_b_email = f"test-patient-b-{uuid.uuid4().hex[:8]}@example.com"
    user_a_id, password_a = await _seed_user(
        test_engine, email=user_a_email, agency_id=agency_id, full_name="A"
    )
    user_b_id, _password_b = await _seed_user(
        test_engine, email=user_b_email, agency_id=agency_id, full_name="B"
    )

    a_n1 = await _insert_notification(
        test_engine, agency_id=agency_id, recipient_user_id=user_a_id, title="N1"
    )
    await _insert_notification(
        test_engine, agency_id=agency_id, recipient_user_id=user_a_id, title="N2"
    )
    await _insert_notification(
        test_engine, agency_id=agency_id, recipient_user_id=user_b_id, title="OtherUser"
    )

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token_a = await _login(client, user_a_email, password_a)
        auth_a = {"Authorization": f"Bearer {token_a}"}

        # List A's notifications
        r = await client.get("/notifications", headers=auth_a)
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["data"]) == 2
        assert body["unread_count"] == 2
        assert body["next_cursor"] is None

        # Mark one as read
        r = await client.patch(f"/notifications/{a_n1}/read", headers=auth_a)
        assert r.status_code == 200, r.text
        assert r.json()["read_at"] is not None

        # unread_count now 1
        r = await client.get("/notifications", headers=auth_a)
        assert r.json()["unread_count"] == 1

        # Mark all read
        r = await client.post("/notifications/read-all", headers=auth_a)
        assert r.status_code == 200, r.text
        assert r.json()["marked_count"] == 1

        # All read now
        r = await client.get("/notifications", headers=auth_a)
        assert r.json()["unread_count"] == 0

        # Idempotent re-mark: still 200, marked_count=0
        r = await client.post("/notifications/read-all", headers=auth_a)
        assert r.json()["marked_count"] == 0

    await test_engine.dispose()


@pytest.mark.asyncio
async def test_unread_only_filter(agency_session) -> None:
    _, _, _, agency_id = agency_session
    test_engine = _make_test_engine()
    user_email = f"test-patient-{uuid.uuid4().hex[:8]}@example.com"
    user_id, password = await _seed_user(
        test_engine, email=user_email, agency_id=agency_id
    )

    read_id = await _insert_notification(
        test_engine, agency_id=agency_id, recipient_user_id=user_id, title="Read"
    )
    await _insert_notification(
        test_engine, agency_id=agency_id, recipient_user_id=user_id, title="Unread"
    )
    # Mark one read via DB
    async with test_engine.begin() as conn:
        await conn.execute(
            text("UPDATE notifications SET read_at = now(), status = 'READ' WHERE id = :i"),
            {"i": read_id},
        )

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, user_email, password)
        auth = {"Authorization": f"Bearer {token}"}

        r = await client.get("/notifications?unread_only=true", headers=auth)
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["data"]) == 1
        assert body["data"][0]["title"] == "Unread"

    await test_engine.dispose()


@pytest.mark.asyncio
async def test_cross_user_isolation(agency_session) -> None:
    _, _, _, agency_id = agency_session
    test_engine = _make_test_engine()

    user_a_email = f"test-patient-a-{uuid.uuid4().hex[:8]}@example.com"
    user_b_email = f"test-patient-b-{uuid.uuid4().hex[:8]}@example.com"
    user_a_id, _password_a = await _seed_user(
        test_engine, email=user_a_email, agency_id=agency_id
    )
    _user_b_id, password_b = await _seed_user(
        test_engine, email=user_b_email, agency_id=agency_id
    )

    a_n = await _insert_notification(
        test_engine, agency_id=agency_id, recipient_user_id=user_a_id
    )

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token_b = await _login(client, user_b_email, password_b)
        auth_b = {"Authorization": f"Bearer {token_b}"}

        # User B listing — should not see A's notification
        r = await client.get("/notifications", headers=auth_b)
        assert r.status_code == 200, r.text
        assert len(r.json()["data"]) == 0

        # User B trying to GET A's notification — 404
        r = await client.get(f"/notifications/{a_n}", headers=auth_b)
        assert r.status_code == 404, r.text

        # User B trying to mark-read A's notification — 404
        r = await client.patch(f"/notifications/{a_n}/read", headers=auth_b)
        assert r.status_code == 404, r.text

    await test_engine.dispose()


@pytest.mark.asyncio
async def test_invalid_cursor_returns_422(agency_session) -> None:
    _, _, _, agency_id = agency_session
    test_engine = _make_test_engine()
    user_email = f"test-patient-{uuid.uuid4().hex[:8]}@example.com"
    _user_id, password = await _seed_user(
        test_engine, email=user_email, agency_id=agency_id
    )

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, user_email, password)
        auth = {"Authorization": f"Bearer {token}"}

        r = await client.get("/notifications?cursor=not-a-cursor", headers=auth)
        assert r.status_code == 422, r.text

    await test_engine.dispose()
