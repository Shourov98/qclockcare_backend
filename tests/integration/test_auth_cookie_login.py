"""Integration tests for cookie-based auth + CSRF protection.

These tests exercise the real FastAPI app via the TestClient. They
need a reachable database (the `client` fixture pings the DB on
lifespan startup). The suite skips them when the DB is unreachable so
unit-only CI runs stay green.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from src.core.database import engine


async def _db_reachable() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _require_db() -> None:
    import asyncio

    if not asyncio.run(_db_reachable()):
        pytest.skip("database not reachable")


async def _seed_user() -> tuple[str, str, str, str]:
    """Insert a minimal ACTIVE user with an AGENCY_ADMIN role.

    Returns (email, password, user_id, agency_id) — the IDs are needed
    by `_cleanup` to wipe in the right order (refresh_tokens BEFORE
    users, agencies last).
    """
    from src.core.security import hash_password

    password = "TestPass123!AB"
    email = f"cookie-test-{uuid.uuid4().hex[:8]}@example.com"
    pw_hash = hash_password(password)
    agency_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    role_id = str(uuid.uuid4())

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO agencies (id, name, timezone) "
                "VALUES (:id, :name, 'America/Chicago')"
            ),
            {"id": agency_id, "name": f"Cookie Test {uuid.uuid4().hex[:6]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, full_name, status, email_verified_at) "
                "VALUES (:id, :email, :pw, 'Cookie Test', 'ACTIVE', now())"
            ),
            {"id": user_id, "email": email, "pw": pw_hash},
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
    """Tear down the seeded rows in an order that survives any
    FK constraints. Refresh tokens first (FK -> users), then
    user_roles, then users, then the agency."""
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL session_replication_role = 'replica'"))
        await conn.execute(
            text("DELETE FROM refresh_tokens WHERE user_id = :u"), {"u": user_id}
        )
        await conn.execute(
            text("DELETE FROM user_roles WHERE user_id = :u"), {"u": user_id}
        )
        await conn.execute(
            text("DELETE FROM users WHERE id = :u"), {"u": user_id}
        )
        await conn.execute(
            text("DELETE FROM agencies WHERE id = :a"), {"a": agency_id}
        )


def test_login_sets_all_three_cookies(client: TestClient) -> None:
    _require_db()
    import asyncio

    email, password, user_id, agency_id = asyncio.run(_seed_user())
    try:
        resp = client.post("/auth/login", json={"email": email, "password": password})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["access_token"]
        assert body["refresh_token"]
        # Cookies should be present on the response.
        set_cookie_headers = resp.headers.getlist("set-cookie")
        joined = "\n".join(set_cookie_headers)
        assert "qc_access=" in joined
        assert "qc_refresh=" in joined
        assert "qc_csrf=" in joined
        # Refresh cookie must be path-scoped to /auth.
        refresh = next(c for c in set_cookie_headers if c.startswith("qc_refresh="))
        assert "Path=/auth" in refresh
        # Access + refresh are HttpOnly; CSRF is NOT HttpOnly (SPA reads it).
        assert "HttpOnly" in next(c for c in set_cookie_headers if c.startswith("qc_access="))
        assert "HttpOnly" in next(c for c in set_cookie_headers if c.startswith("qc_refresh="))
        assert "HttpOnly" not in next(c for c in set_cookie_headers if c.startswith("qc_csrf="))
    finally:
        asyncio.run(_cleanup(user_id, agency_id))


def test_logout_requires_csrf_for_cookie_client(client: TestClient) -> None:
    _require_db()
    import asyncio

    email, password, user_id, agency_id = asyncio.run(_seed_user())
    try:
        login_resp = client.post("/auth/login", json={"email": email, "password": password})
        assert login_resp.status_code == 200

        # Pull the qc_access + qc_csrf cookies back out of the jar.
        cookies = login_resp.cookies
        csrf_value = cookies.get("qc_csrf")
        assert csrf_value

        # Call /auth/logout WITHOUT csrf header — should be 403.
        resp_missing = client.post("/auth/logout", json={})
        assert resp_missing.status_code == 403

        # WITH csrf header — should be 204.
        resp_ok = client.post(
            "/auth/logout",
            json={},
            headers={"X-CSRF-Token": csrf_value},
        )
        assert resp_ok.status_code == 204
    finally:
        asyncio.run(_cleanup(user_id, agency_id))


def test_bearer_logout_bypasses_csrf(client: TestClient) -> None:
    """A client using only `Authorization: Bearer ...` should still be able
    to call `/auth/logout` without an X-CSRF-Token header — that path is
    for cookie-only browsers, not API consumers."""
    _require_db()
    import asyncio

    email, password, user_id, agency_id = asyncio.run(_seed_user())
    try:
        login_resp = client.post("/auth/login", json={"email": email, "password": password})
        assert login_resp.status_code == 200
        body = login_resp.json()
        access_token = body["access_token"]

        # Use ONLY the Authorization header, no X-CSRF-Token.
        resp = client.post(
            "/auth/logout",
            json={},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert resp.status_code == 204
    finally:
        asyncio.run(_cleanup(user_id, agency_id))
