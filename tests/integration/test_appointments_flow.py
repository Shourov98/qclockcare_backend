"""End-to-end appointments + service items flow integration tests against the local Supabase stack.

Walks the agency-admin-side happy path:
    1. AGENCY_ADMIN signs in
    2. Create a patient + a staff member (so we have FK targets)
    3. POST /appointments  → 201 with optional inline service items
    4. GET  /appointments  → list contains it
    5. GET  /appointments/{id}/with-items → returns nested service items
    6. POST /appointments/{id}/assign → assigns staff
    7. POST /appointments/{id}/transition → walks the state machine
    8. POST /appointments/{id}/service-items → adds another item
    9. PATCH /appointments/{id}/service-items/{id} → updates status
   10. POST /appointments/{id}/cancel → cancels (pre-visit only)

Plus negative paths:
    - missing patient_id → 422
    - invalid status transition → 409
    - cancel after CHECKED_IN → 409
    - delete a non-PENDING service item → 409

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


async def _cleanup_agency(test_engine, agency_id: str) -> None:
    async with test_engine.begin() as conn:
        await conn.execute(text("SET LOCAL session_replication_role = 'replica'"))
        # appointments first (they FK to patients/staff)
        await conn.execute(
            text("DELETE FROM appointment_service_items WHERE agency_id = :a"),
            {"a": agency_id},
        )
        await conn.execute(
            text("DELETE FROM appointments WHERE agency_id = :a"),
            {"a": agency_id},
        )
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
            text(
                "DELETE FROM staff_qualifications WHERE agency_id = :a"
            ),
            {"a": agency_id},
        )
        await conn.execute(
            text("DELETE FROM staff_availability WHERE agency_id = :a"),
            {"a": agency_id},
        )
        await conn.execute(
            text("DELETE FROM staff_profiles WHERE agency_id = :a"),
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


async def _login(client: httpx.AsyncClient, email: str, password: str) -> str:
    r = await client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _create_patient(
    client: httpx.AsyncClient, auth: dict
) -> str:
    r = await client.post(
        "/patients",
        json={
            "email": f"pat-{uuid.uuid4().hex[:6]}@example.com",
            "full_name": "Pat",
            "patient_code": f"P-{uuid.uuid4().hex[:6]}",
        },
        headers=auth,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _create_staff(client: httpx.AsyncClient, auth: dict) -> str:
    r = await client.post(
        "/staff",
        json={
            "email": f"staff-{uuid.uuid4().hex[:6]}@example.com",
            "full_name": "Sam Staff",
            "staff_code": f"S-{uuid.uuid4().hex[:6]}",
        },
        headers=auth,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unauthenticated_returns_401() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        r = await client.get("/appointments")
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_create_appointment_with_inline_items(admin_session) -> None:
    """POST /appointments with optional inline service_items returns 201."""
    email, password, _, _ = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        patient_id = await _create_patient(client, auth)
        staff_id = await _create_staff(client, auth)

        r = await client.post(
            "/appointments",
            json={
                "patient_id": patient_id,
                "staff_id": staff_id,
                "scheduled_start": "2026-07-01T09:00:00Z",
                "scheduled_end": "2026-07-01T10:00:00Z",
                "location": "Office",
                "notes": "First visit",
                "service_items": [
                    {
                        "service_type": "PERSONAL_CARE",
                        "planned_minutes": 60,
                        "notes": "Bathing assistance",
                    },
                ],
            },
            headers=auth,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["status"] == "DRAFT"
        assert body["patient_id"] == patient_id
        assert body["staff_id"] == staff_id
        # service_items should be present in the response
        assert body.get("service_items") is not None
        assert len(body["service_items"]) == 1
        assert body["service_items"][0]["service_type"] == "PERSONAL_CARE"


@pytest.mark.asyncio
async def test_list_appointments(admin_session) -> None:
    email, password, _, _ = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        patient_id = await _create_patient(client, auth)

        r = await client.post(
            "/appointments",
            json={
                "patient_id": patient_id,
                "scheduled_start": "2026-07-02T09:00:00Z",
                "scheduled_end": "2026-07-02T10:00:00Z",
            },
            headers=auth,
        )
        appt_id = r.json()["id"]

        r = await client.get("/appointments", headers=auth)
        assert r.status_code == 200, r.text
        body = r.json()
        assert any(a["id"] == appt_id for a in body["data"])


@pytest.mark.asyncio
async def test_get_with_items_returns_nested_collection(admin_session) -> None:
    email, password, _, _ = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        patient_id = await _create_patient(client, auth)
        r = await client.post(
            "/appointments",
            json={
                "patient_id": patient_id,
                "scheduled_start": "2026-07-03T09:00:00Z",
                "scheduled_end": "2026-07-03T10:00:00Z",
                "service_items": [
                    {"service_type": "HOMEMAKING"},
                ],
            },
            headers=auth,
        )
        appt_id = r.json()["id"]

        r = await client.get(f"/appointments/{appt_id}/with-items", headers=auth)
        assert r.status_code == 200, r.text
        items = r.json()["service_items"]
        assert items is not None
        assert len(items) == 1


@pytest.mark.asyncio
async def test_assign_staff(admin_session) -> None:
    email, password, _, _ = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        patient_id = await _create_patient(client, auth)
        staff_id = await _create_staff(client, auth)

        # Create without staff
        r = await client.post(
            "/appointments",
            json={
                "patient_id": patient_id,
                "scheduled_start": "2026-07-04T09:00:00Z",
                "scheduled_end": "2026-07-04T10:00:00Z",
            },
            headers=auth,
        )
        appt_id = r.json()["id"]
        assert r.json()["staff_id"] is None

        # Assign
        r = await client.post(
            f"/appointments/{appt_id}/assign",
            params={"staff_id": staff_id},
            headers=auth,
        )
        assert r.status_code == 200, r.text
        assert r.json()["staff_id"] == staff_id


@pytest.mark.asyncio
async def test_transition_walks_state_machine(admin_session) -> None:
    """Walk DRAFT → SCHEDULED → AWAITING_CONFIRMATION → CONFIRMED → ASSIGNED."""
    email, password, _, _ = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        patient_id = await _create_patient(client, auth)
        staff_id = await _create_staff(client, auth)

        r = await client.post(
            "/appointments",
            json={
                "patient_id": patient_id,
                "staff_id": staff_id,
                "scheduled_start": "2026-07-05T09:00:00Z",
                "scheduled_end": "2026-07-05T10:00:00Z",
            },
            headers=auth,
        )
        appt_id = r.json()["id"]

        # DRAFT → SCHEDULED
        r = await client.post(
            f"/appointments/{appt_id}/transition",
            json={"status": "SCHEDULED"},
            headers=auth,
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "SCHEDULED"

        # SCHEDULED → AWAITING_CONFIRMATION
        r = await client.post(
            f"/appointments/{appt_id}/transition",
            json={"status": "AWAITING_CONFIRMATION"},
            headers=auth,
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "AWAITING_CONFIRMATION"

        # AWAITING_CONFIRMATION → CONFIRMED (with confirmation_side_effects)
        r = await client.post(
            f"/appointments/{appt_id}/transition",
            json={
                "status": "CONFIRMED",
                "confirmation_status": "CONFIRMED",
                "note": "Phone confirmation",
            },
            headers=auth,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "CONFIRMED"
        assert body["confirmation_status"] == "CONFIRMED"
        assert body["confirmation_note"] == "Phone confirmation"
        assert body["confirmed_at"] is not None

        # CONFIRMED → ASSIGNED
        r = await client.post(
            f"/appointments/{appt_id}/transition",
            json={"status": "ASSIGNED"},
            headers=auth,
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ASSIGNED"


@pytest.mark.asyncio
async def test_add_and_complete_service_item(admin_session) -> None:
    email, password, _, _ = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        patient_id = await _create_patient(client, auth)

        r = await client.post(
            "/appointments",
            json={
                "patient_id": patient_id,
                "scheduled_start": "2026-07-06T09:00:00Z",
                "scheduled_end": "2026-07-06T10:00:00Z",
            },
            headers=auth,
        )
        appt_id = r.json()["id"]

        # Add a service item
        r = await client.post(
            f"/appointments/{appt_id}/service-items",
            json={"service_type": "RESPITE", "planned_minutes": 30},
            headers=auth,
        )
        assert r.status_code == 201, r.text
        item_id = r.json()["id"]
        assert r.json()["status"] == "PENDING"

        # Update its status to DONE
        r = await client.patch(
            f"/appointments/{appt_id}/service-items/{item_id}",
            json={"status": "DONE"},
            headers=auth,
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "DONE"


@pytest.mark.asyncio
async def test_cancel_pre_visit_succeeds(admin_session) -> None:
    email, password, _, _ = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        patient_id = await _create_patient(client, auth)
        r = await client.post(
            "/appointments",
            json={
                "patient_id": patient_id,
                "scheduled_start": "2026-07-07T09:00:00Z",
                "scheduled_end": "2026-07-07T10:00:00Z",
            },
            headers=auth,
        )
        appt_id = r.json()["id"]

        r = await client.post(
            f"/appointments/{appt_id}/cancel",
            json={"reason": "Patient unavailable"},
            headers=auth,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "CANCELLED"
        assert body["cancelled_reason"] == "Patient unavailable"
        assert body["cancelled_at"] is not None


# --------------------------------------------------------------------------
# Negative paths
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_missing_patient_id_returns_422(admin_session) -> None:
    email, password, _, _ = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        r = await client.post(
            "/appointments",
            json={
                "scheduled_start": "2026-07-08T09:00:00Z",
                "scheduled_end": "2026-07-08T10:00:00Z",
            },
            headers=auth,
        )
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_unknown_patient_returns_404(admin_session) -> None:
    email, password, _, _ = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        r = await client.post(
            "/appointments",
            json={
                "patient_id": str(uuid.uuid4()),  # bogus
                "scheduled_start": "2026-07-09T09:00:00Z",
                "scheduled_end": "2026-07-09T10:00:00Z",
            },
            headers=auth,
        )
        assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_invalid_status_transition_returns_409(admin_session) -> None:
    """DRAFT → COMPLETED is not a valid edge in the state machine."""
    email, password, _, _ = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        patient_id = await _create_patient(client, auth)
        r = await client.post(
            "/appointments",
            json={
                "patient_id": patient_id,
                "scheduled_start": "2026-07-10T09:00:00Z",
                "scheduled_end": "2026-07-10T10:00:00Z",
            },
            headers=auth,
        )
        appt_id = r.json()["id"]

        r = await client.post(
            f"/appointments/{appt_id}/transition",
            json={"status": "COMPLETED"},
            headers=auth,
        )
        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "INVALID_STATE_TRANSITION"


@pytest.mark.asyncio
async def test_cancel_after_checked_in_returns_409(admin_session) -> None:
    """Once the visit is in flight, cancellation is blocked."""
    email, password, _, _ = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        patient_id = await _create_patient(client, auth)
        staff_id = await _create_staff(client, auth)

        r = await client.post(
            "/appointments",
            json={
                "patient_id": patient_id,
                "staff_id": staff_id,
                "scheduled_start": "2026-07-11T09:00:00Z",
                "scheduled_end": "2026-07-11T10:00:00Z",
            },
            headers=auth,
        )
        appt_id = r.json()["id"]

        # Walk to CHECKED_IN
        for to in ("SCHEDULED", "AWAITING_CONFIRMATION", "CONFIRMED", "ASSIGNED", "CHECKED_IN"):
            r = await client.post(
                f"/appointments/{appt_id}/transition",
                json={"status": to},
                headers=auth,
            )
            assert r.status_code == 200, f"transition to {to} failed: {r.text}"

        r = await client.post(
            f"/appointments/{appt_id}/cancel",
            json={"reason": "Late cancellation"},
            headers=auth,
        )
        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "INVALID_STATE_TRANSITION"


@pytest.mark.asyncio
async def test_delete_non_pending_service_item_returns_409(admin_session) -> None:
    email, password, _, _ = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        patient_id = await _create_patient(client, auth)
        r = await client.post(
            "/appointments",
            json={
                "patient_id": patient_id,
                "scheduled_start": "2026-07-12T09:00:00Z",
                "scheduled_end": "2026-07-12T10:00:00Z",
                "service_items": [{"service_type": "RESPITE"}],
            },
            headers=auth,
        )
        appt_id = r.json()["id"]
        item_id = r.json()["service_items"][0]["id"]

        # Mark it DONE first
        r = await client.patch(
            f"/appointments/{appt_id}/service-items/{item_id}",
            json={"status": "DONE"},
            headers=auth,
        )
        assert r.status_code == 200

        # Now try to delete — should be rejected
        r = await client.delete(
            f"/appointments/{appt_id}/service-items/{item_id}", headers=auth
        )
        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "INVALID_STATE_TRANSITION"


@pytest.mark.asyncio
async def test_unknown_appointment_returns_404(admin_session) -> None:
    email, password, _, _ = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        r = await client.get(
            f"/appointments/{uuid.uuid4()}", headers=auth
        )
        assert r.status_code == 404, r.text