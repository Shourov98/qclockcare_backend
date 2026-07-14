"""End-to-end visits + verification flow integration tests against the local Supabase stack.

Walks the full visit lifecycle:
    1. AGENCY_ADMIN signs in
    2. Create a patient + staff
    3. Create an appointment (so the visit has an appointment_id to attach to)
    4. POST /visits                                  → 201 with seeded service items
    5. PATCH /visits/{id}/transition IN_PROGRESS      → walks state machine
    6. PATCH /visits/{id}/service-items/{id} status=DONE
    7. POST /visits/{id}/notes                        → add note
    8. PATCH /visits/{id}/check-out                   → auto-progresses
    9. PATCH /visits/{id}/transition COMPLETED        → terminal
   10. POST /visits/{id}/verify status=VERIFIED      → file verification
   11. POST /visits/{id}/issues                       → file issue
   12. PATCH /visits/{id}/issues/{id}/resolve         → admin resolves

Plus negative paths:
    - duplicate visit for same appointment → 409
    - NOT_DONE without reason → 422
    - DISPUTED without reason code → 422
    - invalid state transition → 409
    - unknown visit → 404

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
# Per-test engine
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
        # visits come first (FK to appointments)
        await conn.execute(text("DELETE FROM visit_issues WHERE agency_id = :a"), {"a": agency_id})
        await conn.execute(text("DELETE FROM service_verifications WHERE agency_id = :a"), {"a": agency_id})
        await conn.execute(text("DELETE FROM visits WHERE agency_id = :a"), {"a": agency_id})
        await conn.execute(text("DELETE FROM appointments WHERE agency_id = :a"), {"a": agency_id})
        await conn.execute(
            text("DELETE FROM patient_guardian_relationships WHERE agency_id = :a"),
            {"a": agency_id},
        )
        await conn.execute(text("DELETE FROM guardian_profiles WHERE agency_id = :a"), {"a": agency_id})
        await conn.execute(text("DELETE FROM patient_profiles WHERE agency_id = :a"), {"a": agency_id})
        await conn.execute(text("DELETE FROM staff_qualifications WHERE agency_id = :a"), {"a": agency_id})
        await conn.execute(text("DELETE FROM staff_availability WHERE agency_id = :a"), {"a": agency_id})
        await conn.execute(text("DELETE FROM staff_profiles WHERE agency_id = :a"), {"a": agency_id})
        await conn.execute(text("DELETE FROM user_roles WHERE agency_id = :a"), {"a": agency_id})
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


async def _create_patient(client: httpx.AsyncClient, auth: dict) -> str:
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


async def _create_appointment_with_item(
    client: httpx.AsyncClient, auth: dict, patient_id: str, staff_id: str
) -> tuple[str, str]:
    """Create an appointment with one inline service item, return (appointment_id, service_item_id)."""
    r = await client.post(
        "/appointments",
        json={
            "patient_id": patient_id,
            "staff_id": staff_id,
            "scheduled_start": "2026-08-01T09:00:00Z",
            "scheduled_end": "2026-08-01T10:00:00Z",
            "service_items": [{"service_type": "PERSONAL_CARE", "planned_minutes": 60}],
        },
        headers=auth,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"], r.json()["service_items"][0]["id"]


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unauthenticated_returns_401() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        r = await client.get("/visits")
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_create_visit_with_gps_and_seeded_items(admin_session) -> None:
    email, password, _, _ = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        patient_id = await _create_patient(client, auth)
        staff_id = await _create_staff(client, auth)
        appt_id, _ = await _create_appointment_with_item(client, auth, patient_id, staff_id)

        r = await client.post(
            "/visits",
            json={
                "appointment_id": appt_id,
                "check_in_lat": "44.9778",
                "check_in_lng": "-93.2650",
                "check_in_accuracy_m": "5.0",
                "check_in_device_id": "iphone-15-test",
                "check_in_address_match": True,
                "check_in_distance_from_location_m": "12.5",
            },
            headers=auth,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["status"] == "CHECKED_IN"
        assert body["appointment_id"] == appt_id
        assert body["check_in_lat"] == "44.977800"
        assert body["check_in_address_match"] is True
        assert body["check_in_time"] is not None
        # seeded visit_service_items from the appointment
        assert body["service_items"] is not None
        assert len(body["service_items"]) == 1


@pytest.mark.asyncio
async def test_walk_full_visit_lifecycle(admin_session) -> None:
    email, password, _, _ = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        patient_id = await _create_patient(client, auth)
        staff_id = await _create_staff(client, auth)
        appt_id, _appt_item_id = await _create_appointment_with_item(client, auth, patient_id, staff_id)

        # Create visit
        r = await client.post(
            "/visits",
            json={"appointment_id": appt_id},
            headers=auth,
        )
        visit_id = r.json()["id"]
        visit_item_id = r.json()["service_items"][0]["id"]

        # Transition CHECKED_IN → IN_PROGRESS
        r = await client.patch(
            f"/visits/{visit_id}/transition",
            json={"status": "IN_PROGRESS"},
            headers=auth,
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "IN_PROGRESS"

        # Mark the service item DONE
        r = await client.patch(
            f"/visits/{visit_id}/service-items/{visit_item_id}",
            json={"status": "DONE", "note": "Completed without issue"},
            headers=auth,
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "DONE"
        assert r.json()["completed_at"] is not None

        # Add a note
        r = await client.post(
            f"/visits/{visit_id}/notes",
            json={"body": "Patient was in good spirits, ate breakfast."},
            headers=auth,
        )
        assert r.status_code == 201, r.text
        assert r.json()["body"].startswith("Patient was")

        # Check-out (auto-progresses to CHECKED_OUT)
        r = await client.patch(
            f"/visits/{visit_id}/check-out",
            json={
                "check_out_lat": "44.9778",
                "check_out_lng": "-93.2650",
            },
            headers=auth,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "CHECKED_OUT"
        assert body["check_out_time"] is not None
        assert body["duration_seconds"] is not None

        # CHECKED_OUT → COMPLETED
        r = await client.patch(
            f"/visits/{visit_id}/transition",
            json={"status": "COMPLETED"},
            headers=auth,
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_file_verification_verified(admin_session) -> None:
    email, password, _, _ = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        patient_id = await _create_patient(client, auth)
        staff_id = await _create_staff(client, auth)
        appt_id, _ = await _create_appointment_with_item(client, auth, patient_id, staff_id)
        r = await client.post(
            "/visits",
            json={"appointment_id": appt_id},
            headers=auth,
        )
        visit_id = r.json()["id"]

        r = await client.post(
            f"/visits/{visit_id}/verify",
            json={"status": "VERIFIED", "comment": "Everything was great"},
            headers=auth,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "VERIFIED"
        assert body["comment"] == "Everything was great"


@pytest.mark.asyncio
async def test_file_dispute_and_resolve_issue(admin_session) -> None:
    email, password, _, _ = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        patient_id = await _create_patient(client, auth)
        staff_id = await _create_staff(client, auth)
        appt_id, _ = await _create_appointment_with_item(client, auth, patient_id, staff_id)
        r = await client.post(
            "/visits",
            json={"appointment_id": appt_id},
            headers=auth,
        )
        visit_id = r.json()["id"]

        # Dispute the visit
        r = await client.post(
            f"/visits/{visit_id}/verify",
            json={
                "status": "DISPUTED",
                "dispute_reason_code": "STAFF_ARRIVED_LATE",
                "comment": "Caregiver was 45 minutes late",
            },
            headers=auth,
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "DISPUTED"
        assert r.json()["dispute_reason_code"] == "STAFF_ARRIVED_LATE"

        # File an issue
        r = await client.post(
            f"/visits/{visit_id}/issues",
            json={
                "issue_type": "late_arrival",
                "comment": "Called patient to apologise",
            },
            headers=auth,
        )
        assert r.status_code == 201, r.text
        issue_id = r.json()["id"]
        assert r.json()["resolved_at"] is None

        # Resolve the issue
        r = await client.patch(
            f"/visits/{visit_id}/issues/{issue_id}/resolve",
            json={"resolution_note": "Driver rescheduled, future visits on time"},
            headers=auth,
        )
        assert r.status_code == 200, r.text
        assert r.json()["resolved_at"] is not None
        assert r.json()["resolution_note"].startswith("Driver")


# --------------------------------------------------------------------------
# Negative paths
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_duplicate_visit_for_same_appointment_returns_409(admin_session) -> None:
    email, password, _, _ = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        patient_id = await _create_patient(client, auth)
        staff_id = await _create_staff(client, auth)
        appt_id, _ = await _create_appointment_with_item(client, auth, patient_id, staff_id)

        # First visit
        r1 = await client.post("/visits", json={"appointment_id": appt_id}, headers=auth)
        assert r1.status_code == 201

        # Second visit for the same appointment — UNIQUE constraint
        r2 = await client.post("/visits", json={"appointment_id": appt_id}, headers=auth)
        assert r2.status_code == 409, r2.text
        assert r2.json()["error"]["code"] == "DUPLICATE_RESOURCE"


@pytest.mark.asyncio
async def test_not_done_without_reason_returns_422(admin_session) -> None:
    email, password, _, _ = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        patient_id = await _create_patient(client, auth)
        staff_id = await _create_staff(client, auth)
        appt_id, _ = await _create_appointment_with_item(client, auth, patient_id, staff_id)
        r = await client.post("/visits", json={"appointment_id": appt_id}, headers=auth)
        visit_id = r.json()["id"]
        visit_item_id = r.json()["service_items"][0]["id"]

        r = await client.patch(
            f"/visits/{visit_id}/service-items/{visit_item_id}",
            json={"status": "NOT_DONE"},
            headers=auth,
        )
        assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_disputed_without_reason_code_returns_422(admin_session) -> None:
    email, password, _, _ = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        patient_id = await _create_patient(client, auth)
        staff_id = await _create_staff(client, auth)
        appt_id, _ = await _create_appointment_with_item(client, auth, patient_id, staff_id)
        r = await client.post("/visits", json={"appointment_id": appt_id}, headers=auth)
        visit_id = r.json()["id"]

        r = await client.post(
            f"/visits/{visit_id}/verify",
            json={"status": "DISPUTED"},
            headers=auth,
        )
        assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_invalid_state_transition_returns_409(admin_session) -> None:
    """CHECKED_IN → COMPLETED is not a valid edge."""
    email, password, _, _ = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        patient_id = await _create_patient(client, auth)
        staff_id = await _create_staff(client, auth)
        appt_id, _ = await _create_appointment_with_item(client, auth, patient_id, staff_id)
        r = await client.post("/visits", json={"appointment_id": appt_id}, headers=auth)
        visit_id = r.json()["id"]

        r = await client.patch(
            f"/visits/{visit_id}/transition",
            json={"status": "COMPLETED"},
            headers=auth,
        )
        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "INVALID_STATE_TRANSITION"


@pytest.mark.asyncio
async def test_unknown_visit_returns_404(admin_session) -> None:
    email, password, _, _ = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        r = await client.get(f"/visits/{uuid.uuid4()}", headers=auth)
        assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_list_visits_paginated(admin_session) -> None:
    email, password, _, _ = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        patient_id = await _create_patient(client, auth)
        staff_id = await _create_staff(client, auth)

        # Create 3 appointments → 3 visits
        for _ in range(3):
            appt_id, _ = await _create_appointment_with_item(client, auth, patient_id, staff_id)
            r = await client.post("/visits", json={"appointment_id": appt_id}, headers=auth)
            assert r.status_code == 201

        r = await client.get("/visits", headers=auth)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["pagination"]["total"] == 3
        assert len(body["data"]) == 3


@pytest.mark.asyncio
async def test_visit_lat_lng_must_be_paired(admin_session) -> None:
    email, password, _, _ = admin_session
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        token = await _login(client, email, password)
        auth = {"Authorization": f"Bearer {token}"}

        patient_id = await _create_patient(client, auth)
        staff_id = await _create_staff(client, auth)
        appt_id, _ = await _create_appointment_with_item(client, auth, patient_id, staff_id)

        # Only lat, no lng → 422
        r = await client.post(
            "/visits",
            json={"appointment_id": appt_id, "check_in_lat": "44.9778"},
            headers=auth,
        )
        assert r.status_code == 422, r.text
