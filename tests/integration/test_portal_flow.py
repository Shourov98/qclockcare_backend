"""End-to-end portal flow integration tests.

Walks the patient/guardian verify/dispute/report-issue surface:
  1. AGENCY_ADMIN signs in
  2. Create patient + guardian users
  3. Link patient <-> guardian with is_legal=true
  4. Create an appointment + visit (so there's something to verify)
  5. PATIENT logs in, GET /portal/visits → sees the visit
  6. PATIENT POST /portal/visits/{id}/verify → 200, status=VERIFIED
  7. PATIENT POST /portal/visits/{id}/dispute → updates to DISPUTED
  8. PATIENT POST /portal/visits/{id}/report-issue → 201
  9. GUARDIAN logs in, can also see + verify/dispute/report-issue
 10. Unlinked guardian gets 404 (visit not visible)

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


async def _seed_user_with_role(
    test_engine,
    *,
    email: str,
    role: str,
    agency_id: str,
    full_name: str = "Test User",
) -> tuple[str, str]:
    """Insert a user + role row directly, return (user_id, password)."""
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
                "VALUES (:id, :uid, :aid, :r)"
            ),
            {"id": role_id, "uid": user_id, "aid": agency_id, "r": role},
        )
    return user_id, password


async def _cleanup_agency(test_engine, agency_id: str) -> None:
    async with test_engine.begin() as conn:
        await conn.execute(text("SET LOCAL session_replication_role = 'replica'"))
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
        await conn.execute(text("DELETE FROM user_roles WHERE agency_id = :a"), {"a": agency_id})
        await conn.execute(
            text("DELETE FROM users WHERE email LIKE 'test-%@example.com'")
        )


@pytest.fixture
async def agency_session():
    test_engine = _make_test_engine()
    try:
        if not await _db_reachable(test_engine):
            pytest.skip("Database not reachable")
        seed = await _seed_agency_with_admin(test_engine)
        yield seed
        await _cleanup_agency(test_engine, seed[3])
    finally:
        await test_engine.dispose()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
async def _login(client, email: str, password: str) -> str:
    r = await client.post(
        "/auth/login", json={"email": email, "password": password}
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _create_patient(client, auth: dict, *, medical_record_no: str | None = None) -> str:
    r = await client.post(
        "/patients",
        json={
            "first_name": "Pat",
            "last_name": "Patient",
            "medical_record_no": medical_record_no or f"MRN-{uuid.uuid4().hex[:6]}",
        },
        headers=auth,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _create_staff(client, auth: dict) -> str:
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
    client, auth: dict, patient_id: str, staff_id: str
) -> str:
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
    return r.json()["id"]


async def _create_visit_for_appt(client, auth: dict, appt_id: str) -> str:
    r = await client.post(
        "/visits",
        json={
            "appointment_id": appt_id,
            "check_in_lat": "44.9778",
            "check_in_lng": "-93.2650",
            "check_in_accuracy_m": "5.0",
            "check_in_device_id": "iphone-test",
            "check_in_address_match": True,
            "check_in_distance_from_location_m": "12.5",
        },
        headers=auth,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _attach_patient_to_user(
    test_engine,
    *,
    user_id: str,
    patient_id: str,
    agency_id: str,
) -> None:
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE patient_profiles SET user_id = :uid WHERE id = :pid AND agency_id = :aid"
            ),
            {"uid": user_id, "pid": patient_id, "aid": agency_id},
        )


async def _attach_guardian_to_user(
    test_engine,
    *,
    user_id: str,
    guardian_id: str,
    agency_id: str,
) -> None:
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE guardian_profiles SET user_id = :uid WHERE id = :gid AND agency_id = :aid"
            ),
            {"uid": user_id, "gid": guardian_id, "aid": agency_id},
        )


async def _link_guardian_to_patient(
    test_engine,
    *,
    guardian_id: str,
    patient_id: str,
    agency_id: str,
    is_legal: bool = True,
) -> None:
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO patient_guardian_relationships "
                "(id, agency_id, patient_id, guardian_id, relationship_type, is_legal, valid_from) "
                "VALUES (:id, :aid, :pid, :gid, 'GUARDIAN', :legal, '2026-01-01')"
            ),
            {
                "id": str(uuid.uuid4()),
                "aid": agency_id,
                "pid": patient_id,
                "gid": guardian_id,
                "legal": is_legal,
            },
        )


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------
async def test_unauthenticated_returns_401() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        r = await client.get("/portal/visits")
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_patient_lists_and_verifies_own_visit(agency_session) -> None:
    _, _, _, agency_id = agency_session
    test_engine = _make_test_engine()

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        # Admin setup
        admin_email, admin_pw, _, _ = agency_session
        admin_token = await _login(client, admin_email, admin_pw)
        admin_auth = {"Authorization": f"Bearer {admin_token}"}

        patient_id = await _create_patient(client, admin_auth)
        staff_id = await _create_staff(client, admin_auth)
        appt_id = await _create_appointment_with_item(client, admin_auth, patient_id, staff_id)
        visit_id = await _create_visit_for_appt(client, admin_auth, appt_id)

        # Create a PATIENT user and attach them to the patient profile
        patient_email = f"test-patient-{uuid.uuid4().hex[:8]}@example.com"
        patient_user_id, patient_pw = await _seed_user_with_role(
            test_engine,
            email=patient_email,
            role="PATIENT",
            agency_id=agency_id,
            full_name="Pat Patient",
        )
        await _attach_patient_to_user(
            test_engine,
            user_id=patient_user_id,
            patient_id=patient_id,
            agency_id=agency_id,
        )

        # Patient logs in
        pat_token = await _login(client, patient_email, patient_pw)
        pat_auth = {"Authorization": f"Bearer {pat_token}"}

        # List visits
        r = await client.get("/portal/visits", headers=pat_auth)
        assert r.status_code == 200, r.text
        visits = r.json()
        assert len(visits) == 1
        assert visits[0]["id"] == visit_id

        # Get single visit
        r = await client.get(f"/portal/visits/{visit_id}", headers=pat_auth)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["service_items"] is not None
        assert len(body["service_items"]) == 1
        assert body["verification"] is None

        # File positive verification
        r = await client.post(
            f"/portal/visits/{visit_id}/verify",
            json={"comment": "All good."},
            headers=pat_auth,
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "VERIFIED"

        # Idempotent re-call: still 200, still VERIFIED
        r = await client.post(
            f"/portal/visits/{visit_id}/verify",
            json={},
            headers=pat_auth,
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "VERIFIED"

    await test_engine.dispose()


@pytest.mark.asyncio
async def test_patient_files_dispute_then_reports_issue(agency_session) -> None:
    _, _, _, agency_id = agency_session
    test_engine = _make_test_engine()

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        admin_email, admin_pw, _, _ = agency_session
        admin_token = await _login(client, admin_email, admin_pw)
        admin_auth = {"Authorization": f"Bearer {admin_token}"}

        patient_id = await _create_patient(client, admin_auth)
        staff_id = await _create_staff(client, admin_auth)
        appt_id = await _create_appointment_with_item(client, admin_auth, patient_id, staff_id)
        visit_id = await _create_visit_for_appt(client, admin_auth, appt_id)

        patient_email = f"test-patient-{uuid.uuid4().hex[:8]}@example.com"
        patient_user_id, patient_pw = await _seed_user_with_role(
            test_engine,
            email=patient_email,
            role="PATIENT",
            agency_id=agency_id,
        )
        await _attach_patient_to_user(
            test_engine,
            user_id=patient_user_id,
            patient_id=patient_id,
            agency_id=agency_id,
        )
        pat_token = await _login(client, patient_email, patient_pw)
        pat_auth = {"Authorization": f"Bearer {pat_token}"}

        # Dispute with reason
        r = await client.post(
            f"/portal/visits/{visit_id}/dispute",
            json={
                "dispute_reason_code": "SERVICE_NOT_RECEIVED",
                "comment": "Nobody showed up.",
            },
            headers=pat_auth,
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "DISPUTED"
        assert r.json()["dispute_reason_code"] == "SERVICE_NOT_RECEIVED"

        # Report an issue
        r = await client.post(
            f"/portal/visits/{visit_id}/report-issue",
            json={"issue_type": "noise_complaint", "comment": "Construction next door."},
            headers=pat_auth,
        )
        assert r.status_code == 201, r.text
        assert r.json()["issue_type"] == "noise_complaint"

    await test_engine.dispose()


@pytest.mark.asyncio
async def test_dispute_without_reason_returns_422(agency_session) -> None:
    _, _, _, agency_id = agency_session
    test_engine = _make_test_engine()

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        admin_email, admin_pw, _, _ = agency_session
        admin_token = await _login(client, admin_email, admin_pw)
        admin_auth = {"Authorization": f"Bearer {admin_token}"}

        patient_id = await _create_patient(client, admin_auth)
        staff_id = await _create_staff(client, admin_auth)
        appt_id = await _create_appointment_with_item(client, admin_auth, patient_id, staff_id)
        visit_id = await _create_visit_for_appt(client, admin_auth, appt_id)

        patient_email = f"test-patient-{uuid.uuid4().hex[:8]}@example.com"
        patient_user_id, patient_pw = await _seed_user_with_role(
            test_engine, email=patient_email, role="PATIENT", agency_id=agency_id
        )
        await _attach_patient_to_user(
            test_engine,
            user_id=patient_user_id,
            patient_id=patient_id,
            agency_id=agency_id,
        )
        pat_token = await _login(client, patient_email, patient_pw)
        pat_auth = {"Authorization": f"Bearer {pat_token}"}

        # Missing dispute_reason_code
        r = await client.post(
            f"/portal/visits/{visit_id}/dispute",
            json={},
            headers=pat_auth,
        )
        assert r.status_code == 422, r.text

    await test_engine.dispose()


@pytest.mark.asyncio
async def test_unknown_visit_returns_404(agency_session) -> None:
    _, _, _, agency_id = agency_session
    test_engine = _make_test_engine()

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        admin_email, admin_pw, _, _ = agency_session
        admin_token = await _login(client, admin_email, admin_pw)
        admin_auth = {"Authorization": f"Bearer {admin_token}"}

        # Need a patient so the PATIENT user resolves to a valid profile
        patient_id = await _create_patient(client, admin_auth)
        patient_email = f"test-patient-{uuid.uuid4().hex[:8]}@example.com"
        patient_user_id, patient_pw = await _seed_user_with_role(
            test_engine, email=patient_email, role="PATIENT", agency_id=agency_id
        )
        await _attach_patient_to_user(
            test_engine,
            user_id=patient_user_id,
            patient_id=patient_id,
            agency_id=agency_id,
        )
        pat_token = await _login(client, patient_email, patient_pw)
        pat_auth = {"Authorization": f"Bearer {pat_token}"}

        r = await client.get(f"/portal/visits/{uuid.uuid4()}", headers=pat_auth)
        assert r.status_code == 404, r.text

    await test_engine.dispose()


@pytest.mark.asyncio
async def test_unlinked_guardian_gets_404(agency_session) -> None:
    _, _, _, agency_id = agency_session
    test_engine = _make_test_engine()

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        admin_email, admin_pw, _, _ = agency_session
        admin_token = await _login(client, admin_email, admin_pw)
        admin_auth = {"Authorization": f"Bearer {admin_token}"}

        # Patient A + linked visit
        patient_a_id = await _create_patient(client, admin_auth)
        staff_id = await _create_staff(client, admin_auth)
        appt_id = await _create_appointment_with_item(client, admin_auth, patient_a_id, staff_id)
        visit_id = await _create_visit_for_appt(client, admin_auth, appt_id)

        # Guardian user with NO link to this patient
        guardian_email = f"test-guardian-{uuid.uuid4().hex[:8]}@example.com"
        guardian_user_id, guardian_pw = await _seed_user_with_role(
            test_engine,
            email=guardian_email,
            role="GUARDIAN",
            agency_id=agency_id,
        )
        # Create an unrelated guardian profile + link to a DIFFERENT patient so
        # the guardian's profile exists, but the relationship to patient_a is missing.
        _other_patient_id = await _create_patient(client, admin_auth)
        # Attach guardian user to a guardian profile
        async with test_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO guardian_profiles (id, agency_id, user_id, full_name) "
                    "VALUES (:id, :aid, :uid, 'Unlinked Guardian')"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "aid": agency_id,
                    "uid": guardian_user_id,
                },
            )

        guard_token = await _login(client, guardian_email, guardian_pw)
        guard_auth = {"Authorization": f"Bearer {guard_token}"}

        r = await client.get(f"/portal/visits/{visit_id}", headers=guard_auth)
        assert r.status_code == 404, r.text

    await test_engine.dispose()


@pytest.mark.asyncio
async def test_linked_guardian_can_verify(agency_session) -> None:
    _, _, _, agency_id = agency_session
    test_engine = _make_test_engine()

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        admin_email, admin_pw, _, _ = agency_session
        admin_token = await _login(client, admin_email, admin_pw)
        admin_auth = {"Authorization": f"Bearer {admin_token}"}

        patient_id = await _create_patient(client, admin_auth)
        staff_id = await _create_staff(client, admin_auth)
        appt_id = await _create_appointment_with_item(client, admin_auth, patient_id, staff_id)
        visit_id = await _create_visit_for_appt(client, admin_auth, appt_id)

        # Guardian user
        guardian_email = f"test-guardian-{uuid.uuid4().hex[:8]}@example.com"
        guardian_user_id, guardian_pw = await _seed_user_with_role(
            test_engine,
            email=guardian_email,
            role="GUARDIAN",
            agency_id=agency_id,
        )
        guardian_id = str(uuid.uuid4())
        async with test_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO guardian_profiles (id, agency_id, user_id, full_name) "
                    "VALUES (:id, :aid, :uid, 'Linked Guardian')"
                ),
                {"id": guardian_id, "aid": agency_id, "uid": guardian_user_id},
            )
        await _link_guardian_to_patient(
            test_engine,
            guardian_id=guardian_id,
            patient_id=patient_id,
            agency_id=agency_id,
            is_legal=True,
        )

        guard_token = await _login(client, guardian_email, guardian_pw)
        guard_auth = {"Authorization": f"Bearer {guard_token}"}

        r = await client.get(f"/portal/visits/{visit_id}", headers=guard_auth)
        assert r.status_code == 200, r.text

        r = await client.post(
            f"/portal/visits/{visit_id}/verify",
            json={"comment": "All good (via guardian)."},
            headers=guard_auth,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "VERIFIED"
        assert body["verifier_role"] == "GUARDIAN"

    await test_engine.dispose()
