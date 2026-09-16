"""Database-backed integration coverage for the agency dashboard API.

The tests target a running local backend and the same Postgres database used
by it. They skip cleanly when that integration environment is unavailable.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.core.config import settings
from src.core.security import hash_password

BASE_URL = os.environ.get("QLOCKCARE_TEST_URL", "http://127.0.0.1:8001")
PASSWORD = "TestPass123!AB"


def _make_test_engine():
    return create_async_engine(settings.effective_database_url, pool_pre_ping=True, pool_size=2)


async def _db_reachable(engine) -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def _api_reachable() -> bool:
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=2) as client:
            return (await client.get("/health")).status_code == 200
    except httpx.HTTPError:
        return False


async def _add_user(conn, *, agency_id: str, role: str, full_name: str, prefix: str) -> dict[str, str]:
    user_id = str(uuid.uuid4())
    email = f"dashboard-{prefix}-{uuid.uuid4().hex[:8]}@example.com"
    await conn.execute(
        text(
            "INSERT INTO users (id, email, password_hash, full_name, status, email_verified_at) "
            "VALUES (:id, :email, :password_hash, :full_name, 'ACTIVE', now())"
        ),
        {"id": user_id, "email": email, "password_hash": hash_password(PASSWORD), "full_name": full_name},
    )
    await conn.execute(
        text("INSERT INTO user_roles (id, user_id, agency_id, role) VALUES (:id, :user_id, :agency_id, :role)"),
        {"id": str(uuid.uuid4()), "user_id": user_id, "agency_id": agency_id, "role": role},
    )
    return {"id": user_id, "email": email}


async def _seed_agency(conn, *, label: str) -> dict[str, str]:
    agency_id = str(uuid.uuid4())
    await conn.execute(
        text("INSERT INTO agencies (id, name, timezone) VALUES (:id, :name, 'America/Chicago')"),
        {"id": agency_id, "name": f"Dashboard Test {label} {uuid.uuid4().hex[:6]}"},
    )
    admin = await _add_user(conn, agency_id=agency_id, role="AGENCY_ADMIN", full_name=f"Dashboard {label} Admin", prefix=f"{label}-admin")
    staff_user = await _add_user(conn, agency_id=agency_id, role="STAFF", full_name=f"Dashboard {label} Staff", prefix=f"{label}-staff")
    patient_user = await _add_user(conn, agency_id=agency_id, role="PATIENT", full_name=f"Dashboard {label} Patient", prefix=f"{label}-patient")
    staff_id, patient_id = str(uuid.uuid4()), str(uuid.uuid4())
    await conn.execute(
        text(
            "INSERT INTO staff_profiles (id, agency_id, user_id, staff_code, status, hired_at) "
            "VALUES (:id, :agency_id, :user_id, :staff_code, 'ACTIVE', now())"
        ),
        {"id": staff_id, "agency_id": agency_id, "user_id": staff_user["id"], "staff_code": f"DASH-{label}-STAFF"},
    )
    await conn.execute(
        text(
            "INSERT INTO patient_profiles (id, agency_id, user_id, patient_code, status, admitted_at) "
            "VALUES (:id, :agency_id, :user_id, :patient_code, 'ACTIVE', now())"
        ),
        {"id": patient_id, "agency_id": agency_id, "user_id": patient_user["id"], "patient_code": f"DASH-{label}-PATIENT"},
    )
    return {"agency_id": agency_id, "staff_id": staff_id, "patient_id": patient_id, "admin_email": admin["email"], "staff_email": staff_user["email"], "patient_email": patient_user["email"]}


async def _seed_dashboard_records(conn, seed: dict[str, str], *, claim_id: str, include_home: bool = False) -> None:
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    appointment_values = {
        "agency_id": seed["agency_id"],
        "patient_id": seed["patient_id"],
        "staff_id": seed["staff_id"],
        "start": now,
        "end": now + timedelta(hours=1),
    }
    await conn.execute(
        text(
            "INSERT INTO appointments (id, agency_id, patient_id, staff_id, program_type, scheduled_start, scheduled_end, status, billing_status, billing_amount_cents, claim_id) "
            "VALUES (:id, :agency_id, :patient_id, :staff_id, 'PCA', :start, :end, 'COMPLETED', 'paid', 12345, :claim_id)"
        ),
        {**appointment_values, "id": str(uuid.uuid4()), "claim_id": claim_id},
    )
    # This appointment occurs today but must not increase the visits-today KPI.
    await conn.execute(
        text(
            "INSERT INTO appointments (id, agency_id, patient_id, staff_id, program_type, scheduled_start, scheduled_end, status, billing_status, billing_amount_cents) "
            "VALUES (:id, :agency_id, :patient_id, :staff_id, 'PCA', :start, :end, 'CANCELLED', 'cancelled', 9999)"
        ),
        {**appointment_values, "id": str(uuid.uuid4()), "start": now + timedelta(hours=2), "end": now + timedelta(hours=3)},
    )
    if not include_home:
        return
    location_id = str(uuid.uuid4())
    await conn.execute(
        text(
            "INSERT INTO locations (id, agency_id, label, address_line1, city, state, postal_code) "
            "VALUES (:id, :agency_id, 'Dashboard home', '1 Dashboard Way', 'Minneapolis', 'MN', '55401')"
        ),
        {"id": location_id, "agency_id": seed["agency_id"]},
    )
    await conn.execute(
        text(
            "INSERT INTO group_homes (id, agency_id, location_id, latitude, longitude, name, owner_patient_id) "
            "VALUES (:id, :agency_id, :location_id, 44.9778, -93.2650, 'Dashboard Test Home', :patient_id)"
        ),
        {"id": str(uuid.uuid4()), "agency_id": seed["agency_id"], "location_id": location_id, "patient_id": seed["patient_id"]},
    )


async def _cleanup(engine, agency_ids: list[str]) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL session_replication_role = 'replica'"))
        for agency_id in agency_ids:
            await conn.execute(text("DELETE FROM group_homes WHERE agency_id = :agency_id"), {"agency_id": agency_id})
            await conn.execute(text("DELETE FROM locations WHERE agency_id = :agency_id"), {"agency_id": agency_id})
            await conn.execute(text("DELETE FROM appointments WHERE agency_id = :agency_id"), {"agency_id": agency_id})
            await conn.execute(text("DELETE FROM staff_profiles WHERE agency_id = :agency_id"), {"agency_id": agency_id})
            await conn.execute(text("DELETE FROM patient_profiles WHERE agency_id = :agency_id"), {"agency_id": agency_id})
            user_ids = (await conn.execute(text("SELECT user_id FROM user_roles WHERE agency_id = :agency_id"), {"agency_id": agency_id})).scalars().all()
            await conn.execute(text("DELETE FROM user_roles WHERE agency_id = :agency_id"), {"agency_id": agency_id})
            for user_id in user_ids:
                await conn.execute(text("DELETE FROM refresh_tokens WHERE user_id = :user_id"), {"user_id": user_id})
                await conn.execute(text("DELETE FROM users WHERE id = :user_id"), {"user_id": user_id})
            await conn.execute(text("DELETE FROM agencies WHERE id = :agency_id"), {"agency_id": agency_id})


@pytest.fixture
async def dashboard_seed():
    engine = _make_test_engine()
    try:
        if not await _db_reachable(engine):
            pytest.skip("Database not reachable")
        if not await _api_reachable():
            pytest.skip(f"Backend not reachable at {BASE_URL}")
        async with engine.begin() as conn:
            agency_a = await _seed_agency(conn, label="A")
            agency_b = await _seed_agency(conn, label="B")
            await _seed_dashboard_records(conn, agency_a, claim_id="DASH-A-CLAIM", include_home=True)
            await _seed_dashboard_records(conn, agency_b, claim_id="DASH-B-CLAIM")
        yield {"a": agency_a, "b": agency_b}
        await _cleanup(engine, [agency_a["agency_id"], agency_b["agency_id"]])
    finally:
        await engine.dispose()


async def _token(client: httpx.AsyncClient, email: str) -> str:
    response = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


async def test_dashboard_rejects_unauthenticated_and_non_admin_roles(dashboard_seed) -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        assert (await client.get("/dashboard/overview")).status_code == 401
        for email in (dashboard_seed["a"]["staff_email"], dashboard_seed["a"]["patient_email"]):
            auth = {"Authorization": f"Bearer {await _token(client, email)}"}
            assert (await client.get("/dashboard/overview", headers=auth)).status_code == 403
            assert (await client.get("/dashboard/search?q=DASH", headers=auth)).status_code == 403


async def test_dashboard_overview_is_tenant_scoped_and_zero_filled(dashboard_seed) -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        auth = {"Authorization": f"Bearer {await _token(client, dashboard_seed["a"]["admin_email"])}"}
        response = await client.get("/dashboard/overview?months=3", headers=auth)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["active_clients"] == 1
        assert body["active_staff"] == 1
        assert body["visits_today"] == 1
        assert len(body["trends"]) == 3
        assert sum(point["paid_amount_cents"] for point in body["trends"]) == 12345
        assert any(point["paid_amount_cents"] == 0 for point in body["trends"][:-1])


async def test_dashboard_search_returns_only_current_agency_records(dashboard_seed) -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        auth = {"Authorization": f"Bearer {await _token(client, dashboard_seed["a"]["admin_email"])}"}
        response = await client.get("/dashboard/search?q=DASH&limit=20", headers=auth)
        assert response.status_code == 200, response.text
        results = response.json()["results"]
        assert {result["kind"] for result in results} == {"patient", "staff", "appointment", "group_home"}
        assert all("DASH-B" not in f"{result['title']} {result.get('subtitle') or ''}" for result in results)
