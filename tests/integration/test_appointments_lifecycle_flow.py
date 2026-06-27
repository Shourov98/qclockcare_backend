"""End-to-end appointment lifecycle integration tests.

Walks the patient-facing confirmation / reschedule / cancellation flow:

  1. AGENCY_ADMIN pre-creates a patient + an appointment + walks it to
     `AWAITING_CONFIRMATION` via `/transition`.
  2. Patient logs in, POST `/appointments/{id}/confirm` → status flips
     to `CONFIRMED`, a confirmation row exists, an event was appended.
  3. AGENCY_ADMIN GETs `/events` → at least 2 rows
     (initial STATUS_TRANSITION + CONFIRMATION_FILED).
  4. Cross-user isolation: an unrelated patient gets 404 on POST /confirm.
  5. Reschedule request: patient POST `/request-reschedule` → status
     `RESCHEDULE_REQUESTED`, event metadata captures the proposed window.
  6. Cancellation request: patient POST `/request-cancellation` → status
     `CANCELLATION_REQUESTED`, `cancelled_reason` populated.
  7. Admin finalises: POST `/cancel` → status `CANCELLED`, event
     `CANCELLED_BY_ADMIN` appended.
  8. Admin override: AGENCY_ADMIN can `/confirm` on behalf of a patient.

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
# Test helpers (same shape as test_appointments_flow.py)
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


async def _seed_agency_with_admin(test_engine):
    from src.core.security import hash_password

    password = "TestPass123!AB"
    email = f"test-admin-{uuid.uuid4().hex[:8]}@example.com"
    password_hash = hash_password(password)
    agency_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    role_id = str(uuid.uuid4())
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO agencies (id, name, timezone) "
                "VALUES (:id, :name, 'America/Chicago')"
            ),
            {"id": agency_id, "name": f"Test Lifecycle {uuid.uuid4().hex[:6]}"},
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


async def _seed_patient_with_user(test_engine, *, agency_id: str):
    """Create a user with PATIENT role + a patient_profiles row."""
    from src.core.security import hash_password

    user_id = str(uuid.uuid4())
    role_id = str(uuid.uuid4())
    patient_id = str(uuid.uuid4())
    email = f"test-patient-{uuid.uuid4().hex[:8]}@example.com"
    pw = "TestPass123!AB"
    pw_hash = hash_password(pw)
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, full_name, status, email_verified_at) "
                "VALUES (:id, :email, :pw, 'Pat', 'ACTIVE', now())"
            ),
            {"id": user_id, "email": email, "pw": pw_hash},
        )
        await conn.execute(
            text(
                "INSERT INTO user_roles (id, user_id, agency_id, role) "
                "VALUES (:id, :uid, :aid, 'PATIENT')"
            ),
            {"id": role_id, "uid": user_id, "aid": agency_id},
        )
        await conn.execute(
            text(
                "INSERT INTO patient_profiles (id, agency_id, user_id, patient_code, status) "
                "VALUES (:id, :aid, :uid, :code, 'ACTIVE')"
            ),
            {"id": patient_id, "aid": agency_id, "uid": user_id, "code": f"P-{uuid.uuid4().hex[:8]}"},
        )
    return email, pw, user_id, patient_id


async def _cleanup(test_engine, agency_id: str) -> None:
    async with test_engine.begin() as conn:
        await conn.execute(text("SET LOCAL session_replication_role = 'replica'"))
        await conn.execute(
            text("DELETE FROM appointment_events WHERE agency_id = :a"),
            {"a": agency_id},
        )
        await conn.execute(
            text("DELETE FROM appointment_confirmations "
                 "WHERE appointment_id IN (SELECT id FROM appointments WHERE agency_id = :a)"),
            {"a": agency_id},
        )
        await conn.execute(
            text("DELETE FROM appointment_service_items WHERE agency_id = :a"),
            {"a": agency_id},
        )
        await conn.execute(
            text("DELETE FROM appointments WHERE agency_id = :a"),
            {"a": agency_id},
        )
        await conn.execute(
            text("DELETE FROM patient_guardian_relationships WHERE agency_id = :a"),
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
            text("DELETE FROM staff_qualifications WHERE agency_id = :a"),
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


async def _login(client: httpx.AsyncClient, email: str, password: str) -> str:
    r = await client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _create_appointment_and_await_confirmation(
    client: httpx.AsyncClient, auth: dict, patient_id: str
) -> str:
    """Create an appointment then walk it to AWAITING_CONFIRMATION."""
    r = await client.post(
        "/appointments",
        json={
            "patient_id": patient_id,
            "scheduled_start": "2026-07-15T09:00:00Z",
            "scheduled_end": "2026-07-15T10:00:00Z",
        },
        headers=auth,
    )
    assert r.status_code == 201, r.text
    appt_id = r.json()["id"]
    # DRAFT → SCHEDULED → AWAITING_CONFIRMATION
    for status_val in ("SCHEDULED", "AWAITING_CONFIRMATION"):
        r = await client.post(
            f"/appointments/{appt_id}/transition",
            json={"status": status_val},
            headers=auth,
        )
        assert r.status_code == 200, r.text
    return appt_id


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_patient_confirms_appointment(agency_session) -> None:
    admin_email, admin_pw, _admin_id, agency_id = agency_session
    test_engine = _make_test_engine()
    pat_email, pat_pw, _pat_user_id, patient_id = await _seed_patient_with_user(
        test_engine, agency_id=agency_id
    )

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        admin_token = await _login(client, admin_email, admin_pw)
        admin_auth = {"Authorization": f"Bearer {admin_token}"}

        appt_id = await _create_appointment_and_await_confirmation(
            client, admin_auth, patient_id
        )

        # Patient logs in and confirms
        pat_token = await _login(client, pat_email, pat_pw)
        pat_auth = {"Authorization": f"Bearer {pat_token}"}

        r = await client.post(
            f"/appointments/{appt_id}/confirm",
            json={"comment": "See you then"},
            headers=pat_auth,
        )
        assert r.status_code == 200, r.text
        appt, confirmation = r.json()
        assert appt["status"] == "CONFIRMED"
        assert appt["confirmation_status"] == "CONFIRMED"
        assert appt["confirmed_at"] is not None
        assert confirmation["status"] == "CONFIRMED"
        assert confirmation["comment"] == "See you then"
        assert confirmation["confirmation_role"] == "PATIENT"

        # GET /events → at least 2 rows (status transition + confirmation)
        r = await client.get(
            f"/appointments/{appt_id}/events", headers=pat_auth
        )
        assert r.status_code == 200, r.text
        events = r.json()
        event_types = {e["event_type"] for e in events}
        assert "STATUS_TRANSITION" in event_types
        assert "CONFIRMATION_FILED" in event_types

        # GET /confirmation
        r = await client.get(
            f"/appointments/{appt_id}/confirmation", headers=pat_auth
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "CONFIRMED"

    await test_engine.dispose()


@pytest.mark.asyncio
async def test_unrelated_patient_cannot_confirm(agency_session) -> None:
    admin_email, admin_pw, _admin_id, agency_id = agency_session
    test_engine = _make_test_engine()

    # Owner patient + appointment
    _owner_email, _owner_pw, _owner_user_id, owner_patient_id = (
        await _seed_patient_with_user(test_engine, agency_id=agency_id)
    )
    # Another patient (no link to owner)
    other_email, other_pw, _other_user_id, _other_patient_id = (
        await _seed_patient_with_user(test_engine, agency_id=agency_id)
    )

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        admin_token = await _login(client, admin_email, admin_pw)
        admin_auth = {"Authorization": f"Bearer {admin_token}"}

        appt_id = await _create_appointment_and_await_confirmation(
            client, admin_auth, owner_patient_id
        )

        other_token = await _login(client, other_email, other_pw)
        other_auth = {"Authorization": f"Bearer {other_token}"}

        # Other patient tries to confirm owner's appointment
        r = await client.post(
            f"/appointments/{appt_id}/confirm",
            json={"comment": "sneaky"},
            headers=other_auth,
        )
        assert r.status_code == 404, r.text

    await test_engine.dispose()


@pytest.mark.asyncio
async def test_request_reschedule_records_proposed_window(agency_session) -> None:
    admin_email, admin_pw, _admin_id, agency_id = agency_session
    test_engine = _make_test_engine()
    pat_email, pat_pw, _pat_user_id, patient_id = await _seed_patient_with_user(
        test_engine, agency_id=agency_id
    )

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        admin_token = await _login(client, admin_email, admin_pw)
        admin_auth = {"Authorization": f"Bearer {admin_token}"}
        appt_id = await _create_appointment_and_await_confirmation(
            client, admin_auth, patient_id
        )

        pat_token = await _login(client, pat_email, pat_pw)
        pat_auth = {"Authorization": f"Bearer {pat_token}"}

        r = await client.post(
            f"/appointments/{appt_id}/request-reschedule",
            json={
                "proposed_start": "2026-07-16T09:00:00Z",
                "proposed_end": "2026-07-16T10:00:00Z",
                "comment": "Need to move",
            },
            headers=pat_auth,
        )
        assert r.status_code == 200, r.text
        appt = r.json()[0] if isinstance(r.json(), list) else r.json()
        assert appt["status"] == "RESCHEDULE_REQUESTED"

        # Verify event metadata captures the proposal
        r = await client.get(
            f"/appointments/{appt_id}/events", headers=pat_auth
        )
        events = r.json()
        reschedule = next(
            e for e in events if e["event_type"] == "RESCHEDULE_REQUESTED"
        )
        assert "proposed_start" in reschedule["metadata"]
        assert "proposed_end" in reschedule["metadata"]

    await test_engine.dispose()


@pytest.mark.asyncio
async def test_request_then_admin_cancel(agency_session) -> None:
    admin_email, admin_pw, _admin_id, agency_id = agency_session
    test_engine = _make_test_engine()
    pat_email, pat_pw, _pat_user_id, patient_id = await _seed_patient_with_user(
        test_engine, agency_id=agency_id
    )

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        admin_token = await _login(client, admin_email, admin_pw)
        admin_auth = {"Authorization": f"Bearer {admin_token}"}
        appt_id = await _create_appointment_and_await_confirmation(
            client, admin_auth, patient_id
        )

        pat_token = await _login(client, pat_email, pat_pw)
        pat_auth = {"Authorization": f"Bearer {pat_token}"}

        # Patient requests cancellation
        r = await client.post(
            f"/appointments/{appt_id}/request-cancellation",
            json={"reason": "Family emergency"},
            headers=pat_auth,
        )
        assert r.status_code == 200, r.text
        appt = r.json()[0] if isinstance(r.json(), list) else r.json()
        assert appt["status"] == "CANCELLATION_REQUESTED"
        assert appt["cancelled_reason"] == "Family emergency"

        # Admin finalises
        r = await client.post(
            f"/appointments/{appt_id}/cancel",
            json={"reason": "Patient-initiated"},
            headers=admin_auth,
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "CANCELLED"

        # Events: STATUS_TRANSITIONs + CANCELLATION_REQUESTED + CANCELLED_BY_ADMIN
        r = await client.get(
            f"/appointments/{appt_id}/events", headers=admin_auth
        )
        types = [e["event_type"] for e in r.json()]
        assert "CANCELLATION_REQUESTED" in types
        assert "CANCELLED_BY_ADMIN" in types

    await test_engine.dispose()


@pytest.mark.asyncio
async def test_admin_can_confirm_on_behalf(agency_session) -> None:
    """Admin override: AGENCY_ADMIN can /confirm on any appointment."""
    admin_email, admin_pw, _admin_id, agency_id = agency_session
    test_engine = _make_test_engine()
    _pat_email, _pat_pw, _pat_user_id, patient_id = await _seed_patient_with_user(
        test_engine, agency_id=agency_id
    )

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        admin_token = await _login(client, admin_email, admin_pw)
        admin_auth = {"Authorization": f"Bearer {admin_token}"}
        appt_id = await _create_appointment_and_await_confirmation(
            client, admin_auth, patient_id
        )

        r = await client.post(
            f"/appointments/{appt_id}/confirm",
            json={"comment": "Confirmed by admin via phone"},
            headers=admin_auth,
        )
        assert r.status_code == 200, r.text
        appt, confirmation = r.json()
        assert appt["status"] == "CONFIRMED"
        assert confirmation["confirmation_role"] == "PATIENT"

    await test_engine.dispose()


@pytest.mark.asyncio
async def test_unauthenticated_returns_401() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        r = await client.post(
            f"/appointments/{uuid.uuid4()}/confirm",
            json={"comment": "x"},
        )
        assert r.status_code == 401
        r = await client.get(f"/appointments/{uuid.uuid4()}/events")
        assert r.status_code == 401
        r = await client.get(f"/appointments/{uuid.uuid4()}/confirmation")
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_confirm_then_get_events_includes_metadata(agency_session) -> None:
    admin_email, admin_pw, _admin_id, agency_id = agency_session
    test_engine = _make_test_engine()
    pat_email, pat_pw, _pat_user_id, patient_id = await _seed_patient_with_user(
        test_engine, agency_id=agency_id
    )

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        admin_token = await _login(client, admin_email, admin_pw)
        admin_auth = {"Authorization": f"Bearer {admin_token}"}
        appt_id = await _create_appointment_and_await_confirmation(
            client, admin_auth, patient_id
        )

        pat_token = await _login(client, pat_email, pat_pw)
        pat_auth = {"Authorization": f"Bearer {pat_token}"}

        r = await client.post(
            f"/appointments/{appt_id}/confirm",
            json={"declined": True, "comment": "Out of town"},
            headers=pat_auth,
        )
        assert r.status_code == 200, r.text
        # DECLINED does NOT advance the status — it stays AWAITING_CONFIRMATION
        # and the admin still has to /cancel. The confirmation row is recorded.
        appt, confirmation = r.json()
        assert appt["status"] == "AWAITING_CONFIRMATION"
        assert confirmation["status"] == "DECLINED"

        # Event recorded
        r = await client.get(
            f"/appointments/{appt_id}/events", headers=pat_auth
        )
        types = [e["event_type"] for e in r.json()]
        assert "CONFIRMATION_FILED" in types

    await test_engine.dispose()
