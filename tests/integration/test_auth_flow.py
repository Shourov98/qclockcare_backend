"""End-to-end auth flow integration tests against the local Supabase stack.

Walks the full onboarding path:
    1. AGENCY_ADMIN signs in (super admin invited them out-of-band)
    2. POST /auth/login → token pair
    3. POST /auth/me → current user
    4. POST /auth/refresh → new pair (old revoked)
    5. POST /auth/logout → revoked

Plus negative paths:
    - Login with wrong password → 401
    - Login with unknown email → 401
    - Refresh with tampered token → 401
    - Me without bearer → 401

This test is skipped if no local Supabase is reachable.

The `.env` file (DATABASE_URL=...54322) is loaded by `tests/conftest.py`
before this module imports, so the engine pool is built against the right URL.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest
from sqlalchemy import text

from src.core.database import engine
from src.core.security import hash_password


BASE_URL = os.environ.get("QLOCKCARE_TEST_URL", "http://127.0.0.1:8001")


async def _db_reachable() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        print(f"DB NOT REACHABLE: {type(exc).__name__}: {exc}")
        return False


async def _seed_users() -> tuple[str, str, str, str]:
    """Create an agency + AGENCY_ADMIN user (and grant them a role).

    Returns (email, password, user_id, agency_id).
    """
    password = "TestPass123!AB"
    email = f"test-admin-{uuid.uuid4().hex[:8]}@example.com"
    password_hash = hash_password(password)

    async with engine.begin() as conn:
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


async def _cleanup(user_id: str, agency_id: str) -> None:
    """Tear down the test data, bypassing append-only triggers.

    The auth_audit_events table has BEFORE UPDATE/DELETE triggers that
    reject modifications. The FK on `user_id` is ON DELETE SET NULL,
    so deleting a user would update audit rows — blocked by the trigger.
    We disable triggers for the cleanup transaction using
    `session_replication_role = replica` (a standard Postgres pattern).
    """
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL session_replication_role = 'replica'"))
        await conn.execute(text("DELETE FROM refresh_tokens WHERE user_id = :u"), {"u": user_id})
        await conn.execute(text("DELETE FROM user_roles WHERE user_id = :u"), {"u": user_id})
        await conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
        await conn.execute(text("DELETE FROM agencies WHERE id = :a"), {"a": agency_id})


@pytest.fixture
async def fresh_user() -> tuple[str, str, str, str]:
    if not await _db_reachable():
        pytest.skip("Database not reachable")
    seed = await _seed_users()
    yield seed
    await _cleanup(seed[2], seed[3])


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_login_returns_token_pair(fresh_user: tuple[str, str, str, str]) -> None:
    email, password, user_id, _agency_id = fresh_user
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        r = await client.post(
            "/auth/login", json={"email": email, "password": password}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["access_token"]
        assert body["refresh_token"]
        assert body["token_type"] == "bearer"
        assert body["expires_in"] > 0
        assert body["user"]["email"] == email
        assert body["user"]["role"] == "AGENCY_ADMIN"
        assert body["user"]["email_verified"] is True
        assert body["user"]["agency_id"] is not None


@pytest.mark.asyncio
async def test_me_returns_current_user(fresh_user: tuple[str, str, str, str]) -> None:
    email, password, _, _ = fresh_user
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        r = await client.post(
            "/auth/login", json={"email": email, "password": password}
        )
        tokens = r.json()

        r = await client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["user"]["email"] == email


@pytest.mark.asyncio
async def test_refresh_rotates_tokens(fresh_user: tuple[str, str, str, str]) -> None:
    email, password, _, _ = fresh_user
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        r = await client.post(
            "/auth/login", json={"email": email, "password": password}
        )
        first = r.json()

        r = await client.post(
            "/auth/refresh", json={"refresh_token": first["refresh_token"]}
        )
        assert r.status_code == 200, r.text
        second = r.json()
        assert second["access_token"] != first["access_token"]
        assert second["refresh_token"] != first["refresh_token"]

        # Old refresh token is now revoked — second use should fail
        r = await client.post(
            "/auth/refresh", json={"refresh_token": first["refresh_token"]}
        )
        assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_logout_revokes_tokens(fresh_user: tuple[str, str, str, str]) -> None:
    email, password, _, _ = fresh_user
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        r = await client.post(
            "/auth/login", json={"email": email, "password": password}
        )
        tokens = r.json()

        r = await client.post(
            "/auth/logout",
            json={"refresh_token": tokens["refresh_token"]},
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        assert r.status_code == 204

        # Old refresh token now revoked
        r = await client.post(
            "/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert r.status_code == 401


# --------------------------------------------------------------------------
# Negative paths
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_login_wrong_password(fresh_user: tuple[str, str, str, str]) -> None:
    email, _password, _, _ = fresh_user
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        r = await client.post(
            "/auth/login",
            json={"email": email, "password": "WrongPass123!AB"},
        )
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "INVALID_CREDENTIALS"


@pytest.mark.asyncio
async def test_login_unknown_email() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        r = await client.post(
            "/auth/login",
            json={"email": "nobody@example.com", "password": "Whatever123!AB"},
        )
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "INVALID_CREDENTIALS"


@pytest.mark.asyncio
async def test_refresh_tampered_token() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        r = await client.post(
            "/auth/refresh",
            json={"refresh_token": "definitely-not-a-real-jwt"},
        )
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_me_without_bearer_returns_401() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        r = await client.get("/auth/me")
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_forgot_password_always_202() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        r = await client.post(
            "/auth/forgot-password", json={"email": "nobody@example.com"}
        )
        assert r.status_code == 202
        assert r.json() == {"sent": True}