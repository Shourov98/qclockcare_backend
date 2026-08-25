"""Build the Postman collection JSON.

This script is the single source of truth for the Postman collection. It
generates `QlockCare_API.postman_collection.json` so that:

  - All routes across every module are represented (auth, staff, patients,
    appointments, visits, portal, notifications, locations, audit-logs,
    agencies, change-password, admin-tickets, admin-compliance,
    admin-admins, health).
  - Each request carries consistent bearer auth (except the public auth
    and health routes).
  - Each request has 3 test scripts: status code range, envelope shape,
    X-Request-ID round-trip.
  - Each request that produces an ID needed by later requests has an
    auto-extract script that writes to environment variables.
  - The collection-level pre-request script auto-refreshes the access
    token when expired.

Re-run with `uv run python postman/_build_collection.py` after editing any
route definition. The output is committed to the repo.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# Common JS snippets — referenced by id from each request so they don't
# have to be duplicated 99 times in the output JSON.
# --------------------------------------------------------------------------

COLLECTION_ID = "a1c0d5e0-1111-4000-9000-000000000001"

# Runs before every request in the collection.
# Refreshes the access token if it's expired or missing.
COLLECTION_PRE_REQUEST = """
// Auto-refresh: if access_token is missing or expired, try refresh.
// This runs before every request in the collection.
const token = pm.environment.get('access_token');
const expiresAt = parseInt(pm.environment.get('access_token_expires_at') || '0');
if (!token || (expiresAt && Date.now() > expiresAt)) {
    const refresh = pm.environment.get('refresh_token');
    const baseUrl = pm.environment.get('base_url');
    if (refresh && baseUrl) {
        pm.sendRequest({
            url: baseUrl + '/auth/refresh',
            method: 'POST',
            header: {'Content-Type': 'application/json'},
            body: {mode: 'raw', raw: JSON.stringify({refresh_token: refresh})}
        }, (err, res) => {
            if (!err && res.code === 200) {
                const b = res.json();
                pm.environment.set('access_token', b.access_token);
                pm.environment.set('refresh_token', b.refresh_token);
                if (b.expires_in) {
                    pm.environment.set(
                        'access_token_expires_at',
                        String(Date.now() + b.expires_in * 1000)
                    );
                }
            }
        });
    }
}
""".strip()


# The 3 standard tests every request runs after the response.
# `request_name` is captured at definition time for nicer error messages.
def standard_tests(request_name: str) -> str:
    return f"""
pm.test('{request_name} — status is in 2xx', () => {{
    pm.expect(pm.response.code, `expected 2xx, got ${{pm.response.code}}: ${{pm.response.text()}}`)
        .to.be.within(200, 299);
}});

pm.test('{request_name} — response envelope shape', () => {{
    const b = pm.response.json();
    if (pm.response.code >= 400) {{
        pm.expect(b, 'error envelope missing').to.have.property('error');
        pm.expect(b.error).to.have.property('code');
        pm.expect(b.error).to.have.property('message');
        pm.expect(b.error).to.have.property('request_id');
        pm.expect(b.error).to.have.property('timestamp');
    }} else {{
        // Successful responses are either {{data: ...}} or
        // {{data: [...], pagination: ...}} or 204 with empty body.
        if (pm.response.code !== 204) {{
            pm.expect(b, 'success envelope missing data').to.have.property('data');
        }}
    }}
}});

pm.test('{request_name} — X-Request-ID round-trip', () => {{
    const rid = pm.response.headers.get('X-Request-ID');
    pm.expect(rid, 'X-Request-ID header missing').to.be.a('string');
    pm.expect(rid.length, 'X-Request-ID empty').to.be.greaterThan(0);
}});
""".strip()


# Extract the value at `path` from the JSON response and write to env_var.
# Only fires on 2xx.
def extract_id(env_var: str, path: str = "id") -> str:
    return f"""
if (pm.response.code >= 200 && pm.response.code < 300) {{
    const b = pm.response.json();
    const v = {json.dumps(path)}.split('.').reduce(
        (o, k) => (o == null ? o : o[k]), b
    );
    if (v != null) {{
        pm.environment.set('{env_var}', String(v));
    }}
}}
""".strip()


# --------------------------------------------------------------------------
# Helpers for building a request item.
# --------------------------------------------------------------------------


def _example_body(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a Postman `body` block for a JSON request."""
    return {
        "mode": "raw",
        "raw": json.dumps(payload, indent=2),
        "options": {"raw": {"language": "json"}},
    }


def _url(path: str) -> dict[str, Any]:
    """Build a URL block with `{{base_url}}` prefix."""
    return {
        "raw": "{{base_url}}" + path,
        "host": ["{{base_url}}"],
        "path": [p for p in path.split("/") if p],
    }


def _bearer_auth() -> dict[str, Any]:
    return {
        "type": "bearer",
        "bearer": [{"key": "token", "value": "{{access_token}}", "type": "string"}],
    }


def _noauth() -> dict[str, Any]:
    return {"type": "noauth"}


def _common_headers(include_request_id: bool = True) -> list[dict[str, Any]]:
    headers: list[dict[str, Any]] = [
        {"key": "Content-Type", "value": "application/json", "type": "text"},
    ]
    if include_request_id:
        headers.append({"key": "X-Request-ID", "value": "{{$randomUUID}}", "type": "text"})
    return headers


def make_request(
    *,
    name: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    auth: dict[str, Any] | None = None,
    extract: list[tuple[str, str]] | None = None,
    extra_tests: str | None = None,
    multipart: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one Postman request item.

    `multipart` is used for file-upload endpoints (e.g. signature upload).
    Each entry is a formdata item: either a `text` field (`{"key", "type":
    "text", "value": "..."}`) or a `file` field (`{"key", "type": "file",
    "src": []}`). When `multipart` is provided, the JSON `Content-Type`
    header is dropped because the multipart boundary header takes its place.
    """
    auth = auth if auth is not None else _bearer_auth()
    scripts: dict[str, list[str]] = {
        "test": [standard_tests(name)],
    }
    if extract:
        scripts["test"].extend(extract_id(var, json_path) for var, json_path in extract)
    if extra_tests:
        scripts["test"].append(extra_tests)

    headers = _common_headers()
    if multipart is not None:
        # multipart/form-data sets its own Content-Type with the boundary
        # — drop our JSON Content-Type header.
        headers = [h for h in headers if h.get("key") != "Content-Type"]

    item: dict[str, Any] = {
        "name": name,
        "request": {
            "method": method,
            "header": headers,
            "url": _url(path),
            "auth": auth,
        },
        "response": [],
        "event": [
            {"listen": "test", "script": {"type": "text/javascript", "exec": scripts["test"]}},
        ],
    }
    if body is not None:
        item["request"]["body"] = _example_body(body)
    elif multipart is not None:
        item["request"]["body"] = {"mode": "formdata", "formdata": multipart}
    return item


# Reusable multipart body for /visits/{id}/sign — signature image upload
# + optional override for the signer's display name. Postman will prompt
# the user to pick a file when this request is run.
SIGN_VISIT_MULTIPART: list[dict[str, Any]] = [
    {"key": "signature_image", "type": "file", "src": []},
    {"key": "signer_display_name_override", "type": "text", "value": ""},
]


def folder(name: str, items: list[dict[str, Any]], description: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "item": items,
    }


# --------------------------------------------------------------------------
# 1. Auth (9 routes)
# --------------------------------------------------------------------------
AUTH_FOLDER = folder(
    "auth",
    [
        make_request(
            name="Login",
            method="POST",
            path="/auth/login",
            body={
                "email": "admin@qlockcare.dev",
                "password": "AdminDevPass123!",
            },
            auth=_noauth(),
            extract=[
                ("access_token", "access_token"),
                ("refresh_token", "refresh_token"),
                ("user_id", "user.id"),
                ("agency_id", "user.agency_id"),
                ("user_role", "user.role"),
            ],
        ),
        make_request(
            name="Login — wrong password (negative)",
            method="POST",
            path="/auth/login",
            body={"email": "admin@qlockcare.dev", "password": "WrongPassword!"},
            auth=_noauth(),
            extra_tests="""
// Negative path — we override the success-range test for this request.
pm.test('Login (negative) — status is 401', () => {
    pm.expect(pm.response.code).to.equal(401);
});
pm.test('Login (negative) — error.code is INVALID_CREDENTIALS', () => {
    const b = pm.response.json();
    pm.expect(b.error.code).to.equal('INVALID_CREDENTIALS');
});
""",
        ),
        make_request(
            name="Refresh",
            method="POST",
            path="/auth/refresh",
            body={"refresh_token": "{{refresh_token}}"},
            auth=_noauth(),
            extract=[("access_token", "access_token"), ("refresh_token", "refresh_token")],
        ),
        make_request(
            name="Logout",
            method="POST",
            path="/auth/logout",
            body={"refresh_token": "{{refresh_token}}"},
        ),
        make_request(
            name="Get current user (Me)",
            method="GET",
            path="/auth/me",
            extract=[("user_id", "id"), ("agency_id", "agency_id"), ("user_role", "role")],
        ),
        make_request(
            name="Verify email",
            method="POST",
            path="/auth/verify-email",
            body={"user_id": "{{user_id}}", "otp": "0000"},
            auth=_noauth(),
        ),
        make_request(
            name="Resend OTP",
            method="POST",
            path="/auth/resend-otp",
            body={"user_id": "{{user_id}}", "purpose": "EMAIL_VERIFICATION"},
            auth=_noauth(),
        ),
        make_request(
            name="Forgot password",
            method="POST",
            path="/auth/forgot-password",
            body={"email": "admin@qlockcare.dev"},
            auth=_noauth(),
        ),
        make_request(
            name="Reset password",
            method="POST",
            path="/auth/reset-password",
            body={
                "user_id": "{{user_id}}",
                "otp": "0000",
                "new_password": "NewDevPass123!",
            },
            auth=_noauth(),
        ),
        make_request(
            name="Accept invitation",
            method="POST",
            path="/auth/accept-invitation",
            body={
                "email": "<invitee-email>",
                "otp": "000000",
                "password": "InviteePass123!",
            },
            auth=_noauth(),
        ),
    ],
    description="Authentication, OTP verification, password reset. Public endpoints (no Bearer required).",
)

# Note: auth folder has 10 requests because the negative login + accept-invitation
# add an extra. Total still well-aligned with the 9 identity routes (the 10th
# is the negative variant for manual testing).

# --------------------------------------------------------------------------
# 2. Staff (18 routes)
# --------------------------------------------------------------------------
STAFF_FOLDER = folder(
    "staff",
    [
        make_request(
            name="List staff (paginated)",
            method="GET",
            path="/staff?page=1&page_size=20",
        ),
        make_request(
            name="Create staff",
            method="POST",
            path="/staff",
            body={
                "email": "staff-{{$randomUUID}}@qlockcare.dev",
                "full_name": "Alice Caregiver",
                "phone": "+1-555-0100",
                "staff_code": "STF-{{$randomUUID}}",
                "role": "STAFF",
            },
            extract=[("staff_id", "id")],
        ),
        make_request(
            name="Get staff by id",
            method="GET",
            path="/staff/{{staff_id}}",
        ),
        make_request(
            name="Get staff with details",
            method="GET",
            path="/staff/{{staff_id}}/with-details",
        ),
        make_request(
            name="Update staff",
            method="PATCH",
            path="/staff/{{staff_id}}",
            body={"full_name": "Alice C. Updated", "phone": "+1-555-0199"},
        ),
        make_request(
            name="Archive staff (DELETE)",
            method="DELETE",
            path="/staff/{{staff_id}}",
        ),
        make_request(
            name="List qualifications for staff",
            method="GET",
            path="/staff/{{staff_id}}/qualifications",
        ),
        make_request(
            name="Add qualification",
            method="POST",
            path="/staff/{{staff_id}}/qualifications",
            body={
                "qualification_type": "CPR",
                "issued_at": "2026-01-01",
                "expires_at": "2028-01-01",
                "issuer": "American Red Cross",
            },
            extract=[("qualification_id", "id")],
        ),
        make_request(
            name="Update qualification",
            method="PATCH",
            path="/staff/{{staff_id}}/qualifications/{{qualification_id}}",
            body={"status": "VERIFIED"},
        ),
        make_request(
            name="Delete qualification",
            method="DELETE",
            path="/staff/{{staff_id}}/qualifications/{{qualification_id}}",
        ),
        make_request(
            name="Download qualification file",
            method="GET",
            path="/staff/{{staff_id}}/qualifications/{{qualification_id}}/download",
        ),
        make_request(
            name="List availability slots",
            method="GET",
            path="/staff/{{staff_id}}/availability",
        ),
        make_request(
            name="Add availability slot",
            method="POST",
            path="/staff/{{staff_id}}/availability",
            body={
                "day_of_week": "MONDAY",
                "start_time": "08:00",
                "end_time": "12:00",
                "timezone": "America/Chicago",
            },
            extract=[("availability_id", "id")],
        ),
        make_request(
            name="Update availability slot",
            method="PATCH",
            path="/staff/{{staff_id}}/availability/{{availability_id}}",
            body={"start_time": "09:00", "end_time": "13:00"},
        ),
        make_request(
            name="Delete availability slot",
            method="DELETE",
            path="/staff/{{staff_id}}/availability/{{availability_id}}",
        ),
        # Routes below — second-page operations / unique endpoints from
        # staff router. Kept for completeness even when staff_id was just
        # archived (the request will 404, which is expected — auto-recreate
        # by running 'Create staff' first).
        make_request(
            name="Get staff — nonexistent (negative)",
            method="GET",
            path="/staff/00000000-0000-0000-0000-000000000000",
            extra_tests="""pm.test('404 for missing staff', () => {
    pm.expect(pm.response.code).to.equal(404);
});""",
        ),
    ],
    description="Care staff profiles, qualifications, and availability slots. Requires AGENCY_ADMIN or SUPER_ADMIN role.",
)

# --------------------------------------------------------------------------
# 3. Patients + Guardians (16 routes)
# --------------------------------------------------------------------------
PATIENTS_FOLDER = folder(
    "patients",
    [
        make_request(
            name="List patients (paginated)",
            method="GET",
            path="/patients?page=1&page_size=20",
        ),
        make_request(
            name="Create patient",
            method="POST",
            path="/patients",
            body={
                "patient_code": "PAT-{{$randomUUID}}",
                "first_name": "Bob",
                "last_name": "Patient",
                "date_of_birth": "1985-03-14",
                "email": "patient-{{$randomUUID}}@qlockcare.dev",
                "phone": "+1-555-0200",
            },
            extract=[("patient_id", "id")],
        ),
        make_request(
            name="Get patient by id",
            method="GET",
            path="/patients/{{patient_id}}",
        ),
        make_request(
            name="Get patient with relationships",
            method="GET",
            path="/patients/{{patient_id}}/with-relationships",
        ),
        make_request(
            name="Update patient",
            method="PATCH",
            path="/patients/{{patient_id}}",
            body={"phone": "+1-555-0299"},
        ),
        make_request(
            name="Archive patient (DELETE)",
            method="DELETE",
            path="/patients/{{patient_id}}",
        ),
        make_request(
            name="Link guardian to patient",
            method="POST",
            path="/patients/{{patient_id}}/guardians",
            body={
                "first_name": "Maria",
                "last_name": "Guardian",
                "email": "guardian-{{$randomUUID}}@qlockcare.dev",
                "phone": "+1-555-0300",
                "relationship_type": "MOTHER",
            },
            extract=[("guardian_id", "id"), ("relationship_id", "id")],
        ),
        make_request(
            name="List guardians for patient",
            method="GET",
            path="/patients/{{patient_id}}/guardians",
        ),
        make_request(
            name="Create standalone guardian",
            method="POST",
            path="/guardians",
            body={
                "first_name": "Standalone",
                "last_name": "Guardian",
                "email": "guardian-standalone-{{$randomUUID}}@qlockcare.dev",
                "phone": "+1-555-0400",
            },
            extract=[("guardian_id", "id")],
        ),
        make_request(
            name="List guardians (paginated)",
            method="GET",
            path="/guardians?page=1&page_size=20",
        ),
        make_request(
            name="Get guardian by id",
            method="GET",
            path="/guardians/{{guardian_id}}",
        ),
        make_request(
            name="Update guardian",
            method="PATCH",
            path="/guardians/{{guardian_id}}",
            body={"phone": "+1-555-0499"},
        ),
        make_request(
            name="Delete guardian",
            method="DELETE",
            path="/guardians/{{guardian_id}}",
        ),
        make_request(
            name="Update relationship",
            method="PATCH",
            path="/patient-guardian-relationships/{{relationship_id}}",
            body={"relationship_type": "FATHER"},
        ),
        make_request(
            name="Delete relationship",
            method="DELETE",
            path="/patient-guardian-relationships/{{relationship_id}}",
        ),
        make_request(
            name="Create patient — duplicate code (negative)",
            method="POST",
            path="/patients",
            body={
                "patient_code": "PAT-DUPLICATE",
                "first_name": "Dup",
                "last_name": "Patient",
                "date_of_birth": "1990-01-01",
            },
            extra_tests="""pm.test('409 for duplicate patient_code', () => {
    pm.expect(pm.response.code).to.equal(409);
});""",
        ),
    ],
    description="Patient profiles, standalone guardians, and patient-guardian relationships. Requires AGENCY_ADMIN.",
)

# --------------------------------------------------------------------------
# 4. Appointments (spec-aligned: SCHEDULED→READY→IN_PROGRESS→AWAITING_SIGNATURE→COMPLETED)
# --------------------------------------------------------------------------
APPOINTMENTS_FOLDER = folder(
    "appointments",
    [
        make_request(
            name="List appointments (paginated)",
            method="GET",
            path="/appointments?page=1&page_size=20",
        ),
        make_request(
            name="Create appointment",
            method="POST",
            path="/appointments",
            body={
                "patient_id": "{{patient_id}}",
                "scheduled_start": "2026-07-01T10:00:00Z",
                "scheduled_end": "2026-07-01T11:00:00Z",
                "location_id": "{{location_id}}",
                "notes": "Initial assessment",
            },
            extract=[("appointment_id", "id")],
        ),
        make_request(
            name="Get appointment with items",
            method="GET",
            path="/appointments/{{appointment_id}}/with-items",
        ),
        make_request(
            name="Get appointment by id",
            method="GET",
            path="/appointments/{{appointment_id}}",
        ),
        make_request(
            name="Update appointment",
            method="PATCH",
            path="/appointments/{{appointment_id}}",
            body={"notes": "Updated notes"},
        ),
        make_request(
            name="Cancel appointment",
            method="POST",
            path="/appointments/{{appointment_id}}/cancel",
            body={"reason": "Patient unavailable"},
        ),
        make_request(
            name="Transition appointment state",
            method="POST",
            path="/appointments/{{appointment_id}}/transition",
            body={"to_status": "READY"},
        ),
        make_request(
            name="Assign staff to appointment",
            method="POST",
            path="/appointments/{{appointment_id}}/assign",
            body={"staff_id": "{{staff_id}}", "role": "PRIMARY"},
        ),
        make_request(
            name="Mark appointment ready",
            method="POST",
            path="/appointments/{{appointment_id}}/ready",
            body={},
        ),
        make_request(
            name="List activities for appointment",
            method="GET",
            path="/appointments/{{appointment_id}}/activities",
        ),
        make_request(
            name="Add activity",
            method="POST",
            path="/appointments/{{appointment_id}}/activities",
            body={
                "name": "Check blood pressure",
                "planned_minutes": 5,
                "notes": "Use the wrist cuff on the nightstand.",
            },
            extract=[("activity_id", "id")],
        ),
        make_request(
            name="Update activity",
            method="PATCH",
            path="/appointments/{{appointment_id}}/activities/{{activity_id}}",
            body={"status": "DONE", "notes": "BP 128/82"},
        ),
        make_request(
            name="Delete activity",
            method="DELETE",
            path="/appointments/{{appointment_id}}/activities/{{activity_id}}",
        ),
    ],
    description=(
        "Care appointments — scheduling + 5-state lifecycle "
        "(SCHEDULED→READY→IN_PROGRESS→AWAITING_SIGNATURE→COMPLETED) "
        "plus free-text activities (renamed from enum service-items)."
    ),
)

# --------------------------------------------------------------------------
# 5. Visits (spec-aligned — EVV split into start/end, signature replaces verify)
# --------------------------------------------------------------------------
VISITS_FOLDER = folder(
    "visits",
    [
        make_request(
            name="Create visit (from appointment)",
            method="POST",
            path="/visits",
            body={
                "appointment_id": "{{appointment_id}}",
                "staff_id": "{{staff_id}}",
            },
            extract=[("visit_id", "id")],
        ),
        make_request(
            name="Get visit by id",
            method="GET",
            path="/visits/{{visit_id}}",
        ),
        make_request(
            name="Get visit with items",
            method="GET",
            path="/visits/{{visit_id}}/with-items",
        ),
        make_request(
            name="List visits (paginated)",
            method="GET",
            path="/visits?page=1&page_size=20",
        ),
        make_request(
            name="Transition visit state",
            method="POST",
            path="/visits/{{visit_id}}/transition",
            body={"to_status": "IN_PROGRESS"},
        ),
        make_request(
            name="Confirm visit billing",
            method="POST",
            path="/visits/{{visit_id}}/confirm-billing",
            body={},
        ),
        make_request(
            name="End visit (record EVV end)",
            method="PATCH",
            path="/visits/{{visit_id}}/end",
            body={
                "end_time": "2026-07-01T11:02:00Z",
                "end_lat": 44.9778,
                "end_lng": -93.2650,
                "end_accuracy_m": 12.5,
            },
        ),
        make_request(
            name="Sign visit (upload signature)",
            method="POST",
            path="/visits/{{visit_id}}/sign",
            multipart=SIGN_VISIT_MULTIPART,
            extract=[("signature_id", "id")],
        ),
        make_request(
            name="List visit activities",
            method="GET",
            path="/visits/{{visit_id}}/activities",
        ),
        make_request(
            name="Add visit activity",
            method="POST",
            path="/visits/{{visit_id}}/activities",
            body={
                "name": "Bathing assistance",
                "planned_minutes": 30,
                "notes": "Patient requested warm water.",
            },
            extract=[("activity_id", "id")],
        ),
        make_request(
            name="Update visit activity",
            method="PATCH",
            path="/visits/{{visit_id}}/activities/{{activity_id}}",
            body={"status": "DONE", "notes": "Completed at 11:00"},
        ),
        make_request(
            name="Delete visit activity",
            method="DELETE",
            path="/visits/{{visit_id}}/activities/{{activity_id}}",
        ),
        make_request(
            name="List visit notes",
            method="GET",
            path="/visits/{{visit_id}}/notes",
        ),
        make_request(
            name="Add visit note",
            method="POST",
            path="/visits/{{visit_id}}/notes",
            body={
                "note_text": "Patient in good spirits. Vital signs normal.",
                "category": "CLINICAL",
            },
        ),
        # Live GPS — staff opt-in EVV location sharing. The staff mobile
        # app calls `start-location-sharing` once when the user toggles
        # sharing on, then `location-ping` ~every 15 s while sharing.
        # `stop-location-sharing` is the opt-out. All three are
        # POST-only, idempotent, and return the visit row so the SPA can
        # refresh `live_lat` / `live_lng` / `live_ping_at` from the
        # response without a second GET.
        make_request(
            name="Start location sharing for visit",
            method="POST",
            path="/visits/{{visit_id}}/start-location-sharing",
            body={
                "initial_lat": 44.9778,
                "initial_lng": -93.2650,
                "initial_accuracy_m": 12.5,
            },
        ),
        make_request(
            name="Send location ping for visit",
            method="POST",
            path="/visits/{{visit_id}}/location-ping",
            body={
                "lat": 44.9778,
                "lng": -93.2650,
                "accuracy_m": 12.5,
                "device_id": "ios-abc-123",
            },
        ),
        make_request(
            name="Stop location sharing for visit",
            method="POST",
            path="/visits/{{visit_id}}/stop-location-sharing",
        ),
    ],
    description=(
        "Field visits by care staff — start/end via EVV records, "
        "billing confirmation, signature upload, free-text activities, "
        "notes, and live GPS sharing."
    ),
)

# --------------------------------------------------------------------------
# 6. Portal (3 routes — PATIENT role, spec-aligned)
# --------------------------------------------------------------------------
PORTAL_FOLDER = folder(
    "portal",
    [
        make_request(
            name="List my visits (PATIENT)",
            method="GET",
            path="/portal/visits",
        ),
        make_request(
            name="Get my visit detail (PATIENT)",
            method="GET",
            path="/portal/visits/{{visit_id}}",
        ),
        make_request(
            name="Report issue on my visit (PATIENT)",
            method="POST",
            path="/portal/visits/{{visit_id}}/report-issue",
            body={"severity": "LOW", "description": "Caregiver was 15 min late"},
        ),
    ],
    description=(
        "Patient-facing endpoints. Run 'auth > Login as PATIENT' first "
        "(after seeding via scripts/seed_test_user.py) — the request "
        "path will 403 with AGENCY_ADMIN. Signature is submitted via "
        "POST /visits/{id}/sign from any role."
    ),
)

# --------------------------------------------------------------------------
# 7. Notifications (8 routes)
# --------------------------------------------------------------------------
NOTIFICATIONS_FOLDER = folder(
    "notifications",
    [
        make_request(
            name="List my notifications",
            method="GET",
            path="/notifications?page=1&page_size=20",
        ),
        make_request(
            name="Get unread badge count",
            method="GET",
            path="/notifications/badge",
        ),
        make_request(
            name="Get notification by id",
            method="GET",
            path="/notifications/{{notification_id}}",
        ),
        make_request(
            name="Mark notification as read",
            method="PATCH",
            path="/notifications/{{notification_id}}/read",
            body={"read": True},
        ),
        make_request(
            name="Mark all notifications as read",
            method="POST",
            path="/notifications/read-all",
            body={},
        ),
        make_request(
            name="Get my preferences",
            method="GET",
            path="/notifications/preferences",
        ),
        make_request(
            name="Update a preference",
            method="PUT",
            path="/notifications/preferences/APPOINTMENT_REMINDER/EMAIL",
            body={"enabled": True, "channels": ["EMAIL", "PUSH"]},
        ),
        make_request(
            name="Send broadcast (AGENCY_ADMIN)",
            method="POST",
            path="/notifications/broadcast",
            body={
                "subject": "All-hands meeting Friday",
                "body": "Reminder: all-hands at 3 PM Friday.",
                "audience": {"role": "STAFF", "agency_id": "{{agency_id}}"},
                "channels": ["EMAIL"],
            },
        ),
    ],
    description="Per-user notifications, badge counts, broadcast (admin-only), and channel preferences.",
)

# --------------------------------------------------------------------------
# 8. Locations (5 routes)
# --------------------------------------------------------------------------
LOCATIONS_FOLDER = folder(
    "locations",
    [
        make_request(
            name="List locations",
            method="GET",
            path="/locations",
        ),
        make_request(
            name="Create location",
            method="POST",
            path="/locations",
            body={
                "name": "Main Office",
                "address_line1": "123 Main St",
                "city": "Minneapolis",
                "state": "MN",
                "postal_code": "55401",
                "country": "US",
                "timezone": "America/Chicago",
            },
            extract=[("location_id", "id")],
        ),
        make_request(
            name="Get location by id",
            method="GET",
            path="/locations/{{location_id}}",
        ),
        make_request(
            name="Update location",
            method="PATCH",
            path="/locations/{{location_id}}",
            body={"name": "Main Office (HQ)"},
        ),
        make_request(
            name="Archive location (DELETE)",
            method="DELETE",
            path="/locations/{{location_id}}",
        ),
    ],
    description="Agency locations — used as visit/appointment venues.",
)

# --------------------------------------------------------------------------
# 9. Audit Logs (2 routes)
# --------------------------------------------------------------------------
AUDIT_LOGS_FOLDER = folder(
    "audit-logs",
    [
        make_request(
            name="List audit logs (paginated, filterable)",
            method="GET",
            path="/audit-logs?page=1&page_size=20",
        ),
        make_request(
            name="Get audit log by id",
            method="GET",
            path="/audit-logs/00000000-0000-0000-0000-000000000000",
        ),
    ],
    description="Append-only audit trail. Filter by actor / resource / time range. Requires SUPER_ADMIN.",
)

# --------------------------------------------------------------------------
# 10. Agencies (6 routes — SUPER_ADMIN only)
# --------------------------------------------------------------------------
AGENCIES_FOLDER = folder(
    "agencies",
    [
        make_request(
            name="List agencies (paginated)",
            method="GET",
            path="/agencies?page=1&page_size=20",
        ),
        make_request(
            name="Create agency",
            method="POST",
            path="/agencies",
            body={
                "name": "Test Agency {{$randomUUID}}",
                "timezone": "America/Chicago",
                "settings": {"theme": "light"},
                "initial_program_codes": ["PCA", "ARMHS"],
                "admin": {
                    "email": "agencyadmin{{$randomUUID}}@qlockcare.dev",
                    "full_name": "Agency Admin",
                    # `password` omitted → admin is created in INVITED
                    # status and an invitation email is scheduled. Copy
                    # the OTP from the terminal log line
                    # `auth.email.dev_invitation_for_test_only` and
                    # submit it via `POST /auth/accept-invitation`.
                },
            },
            extract=[("agency_id", "id")],
        ),
        make_request(
            name="Get agency by id",
            method="GET",
            path="/agencies/{{agency_id}}",
        ),
        make_request(
            name="Add agency admin (orphan remediation)",
            method="POST",
            path="/agencies/{{agency_id}}/admins",
            body={
                "email": "agencyadmin{{$randomUUID}}@qlockcare.dev",
                "full_name": "Agency Admin",
                # `password` omitted → admin is created in INVITED
                # status. Copy the OTP from the terminal log line
                # `auth.email.dev_invitation_for_test_only` and submit
                # it via `POST /auth/accept-invitation`.
            },
        ),
        make_request(
            name="Get deleted agency by id (?include_deleted=true)",
            method="GET",
            path="/agencies/{{agency_id}}?include_deleted=true",
        ),
        make_request(
            name="Patch agency (rename + status flip)",
            method="PATCH",
            path="/agencies/{{agency_id}}",
            body={"name": "Renamed {{$randomUUID}}", "status": "SUSPENDED"},
        ),
        make_request(
            name="Soft-delete agency",
            method="DELETE",
            path="/agencies/{{agency_id}}",
        ),
        make_request(
            name="List programs the agency offers",
            method="GET",
            path="/agencies/{{agency_id}}/programs",
        ),
    ],
    description=(
        "Agency-tenant management (SUPER_ADMIN only). Create / list / patch / "
        "soft-delete agencies, and list the programs each agency offers. "
        "Auto-extracts `agency_id` from the Create response for use by the "
        "downstream Get/Patch/Delete/Programs requests."
    ),
)

# --------------------------------------------------------------------------
# 11. Change Password (1 route — authed)
# --------------------------------------------------------------------------
CHANGE_PASSWORD_FOLDER = folder(
    "change-password",
    [
        make_request(
            name="Change password (authed)",
            method="POST",
            path="/auth/change-password",
            body={
                "current_password": "OldDevPass123!",
                "new_password": "NewDevPass456!",
            },
        ),
    ],
    description=(
        "Authenticated password rotation. Caller must supply the *current* "
        "password (re-authentication guard). On success the server revokes ALL "
        "outstanding refresh tokens, forcing re-login on every other device. "
        "The current browser continues to work until its access token expires."
    ),
)


# --------------------------------------------------------------------------
# 12. Admin — Tickets (9 routes — SUPPORT scope)
# --------------------------------------------------------------------------
ADMIN_TICKETS_FOLDER = folder(
    "admin-tickets",
    [
        make_request(
            name="Ticket stats",
            method="GET",
            path="/admin/tickets/stats",
        ),
        make_request(
            name="List tickets (paginated)",
            method="GET",
            path="/admin/tickets?page=1&page_size=20",
        ),
        make_request(
            name="Filter tickets by status",
            method="GET",
            path="/admin/tickets?page=1&page_size=20&status=OPEN",
        ),
        make_request(
            name="Search tickets",
            method="GET",
            path="/admin/tickets?page=1&page_size=20&search=stripe",
        ),
        make_request(
            name="Get ticket by id",
            method="GET",
            path="/admin/tickets/{{ticket_id}}",
        ),
        make_request(
            name="Create ticket",
            method="POST",
            path="/admin/tickets",
            body={
                "title": "Stripe webhook deliveries dropped at 14:32 UTC",
                "description": "Customer portal stopped receiving invoice.paid events.",
                "priority": "HIGH",
                "agency_id": None,
                "assignee_user_id": None,
            },
            extract=[("ticket_id", "id")],
        ),
        make_request(
            name="Update ticket",
            method="PATCH",
            path="/admin/tickets/{{ticket_id}}",
            body={"status": "IN_PROGRESS", "priority": "CRITICAL"},
        ),
        make_request(
            name="Soft-delete ticket",
            method="DELETE",
            path="/admin/tickets/{{ticket_id}}",
        ),
        make_request(
            name="Add ticket comment",
            method="POST",
            path="/admin/tickets/{{ticket_id}}/comments",
            body={"body": "Reproduced on staging — opening incident.", "kind": "COMMENT"},
        ),
    ],
    description=(
        "Internal admin support tickets. SUPER_ADMIN or PLATFORM_ADMIN with "
        "SUPPORT scope. Not tenant-scoped. Status/priority changes and "
        "assignee updates auto-log timeline entries via PATCH."
    ),
)


# --------------------------------------------------------------------------
# 13. Admin — Compliance (12 routes — AGENCIES scope)
# --------------------------------------------------------------------------
ADMIN_COMPLIANCE_FOLDER = folder(
    "admin-compliance",
    [
        make_request(
            name="Compliance stats",
            method="GET",
            path="/admin/compliance/stats",
        ),
        make_request(
            name="List documents (paginated)",
            method="GET",
            path="/admin/compliance/documents?page=1&page_size=20",
        ),
        make_request(
            name="List documents by agency",
            method="GET",
            path="/admin/compliance/documents?page=1&page_size=20&agency_id={{agency_id}}",
        ),
        make_request(
            name="Missing documents report",
            method="GET",
            path="/admin/compliance/documents/missing?page=1&page_size=20",
        ),
        make_request(
            name="Create document",
            method="POST",
            path="/admin/compliance/documents",
            body={
                "agency_id": "{{agency_id}}",
                "name": "Annual HIPAA training certificate",
                "doc_type": "CERTIFICATE",
                "status": "MISSING",
                "description": "Required for all staff before patient contact.",
                "expires_at": None,
                "file_url": None,
            },
            extract=[("document_id", "id")],
        ),
        make_request(
            name="Update document",
            method="PATCH",
            path="/admin/compliance/documents/{{document_id}}",
            body={"status": "VALID", "expires_at": "2027-01-01T00:00:00Z"},
        ),
        make_request(
            name="Soft-delete document",
            method="DELETE",
            path="/admin/compliance/documents/{{document_id}}",
        ),
        make_request(
            name="List licenses (paginated)",
            method="GET",
            path="/admin/compliance/licenses?page=1&page_size=20",
        ),
        make_request(
            name="Filter licenses by CRITICAL status",
            method="GET",
            path="/admin/compliance/licenses?page=1&page_size=20&status=CRITICAL",
        ),
        make_request(
            name="Create license",
            method="POST",
            path="/admin/compliance/licenses",
            body={
                "agency_id": "{{agency_id}}",
                "name": "State Operating License",
                "doc_type": "LICENSE",
                "status": None,
                "issued_at": "2025-01-15T00:00:00Z",
                "expires_at": "2026-09-30T00:00:00Z",
                "reference_number": "ST-OPL-2025-001",
                "notes": None,
            },
            extract=[("license_id", "id")],
        ),
        make_request(
            name="Update license",
            method="PATCH",
            path="/admin/compliance/licenses/{{license_id}}",
            body={"expires_at": "2027-09-30T00:00:00Z", "reference_number": "ST-OPL-2026-001"},
        ),
        make_request(
            name="Soft-delete license",
            method="DELETE",
            path="/admin/compliance/licenses/{{license_id}}",
        ),
    ],
    description=(
        "Per-agency required documents and expiring licenses. SUPER_ADMIN or "
        "PLATFORM_ADMIN with AGENCIES scope. Status is auto-derived from "
        "expires_at when not supplied on create/update."
    ),
)


# --------------------------------------------------------------------------
# 14. Admin — Admins (5 routes — SUPER_ADMIN only for write paths)
# --------------------------------------------------------------------------
ADMIN_ADMINS_FOLDER = folder(
    "admin-admins",
    [
        make_request(
            name="List admins",
            method="GET",
            path="/admin/admins?page=1&page_size=20",
        ),
        make_request(
            name="Get admin by id",
            method="GET",
            path="/admin/admins/{{admin_id}}",
        ),
        make_request(
            name="Create PLATFORM_ADMIN with scopes",
            method="POST",
            path="/admin/admins",
            body={
                "full_name": "New Platform Admin",
                "email": "new-admin+{{$randomUUID}}@example.com",
                "phone": None,
                "scopes": ["SUPPORT", "AGENCIES"],
            },
            extract=[("admin_id", "id")],
        ),
        make_request(
            name="Update admin (rename + replace scopes)",
            method="PATCH",
            path="/admin/admins/{{admin_id}}",
            body={"full_name": "Renamed Admin", "scopes": ["SUPPORT"]},
        ),
        make_request(
            name="Archive admin (DELETE)",
            method="DELETE",
            path="/admin/admins/{{admin_id}}",
        ),
    ],
    description=(
        "Platform admin management. List/get are open to SUPER_ADMIN + "
        "PLATFORM_ADMIN; create / delete are SUPER_ADMIN only. PLATFORM_ADMIN "
        "creation requires at least one scope. Updating `scopes` on a "
        "SUPER_ADMIN is silently ignored."
    ),
)


# --------------------------------------------------------------------------
# 15. Health (2 routes)
# --------------------------------------------------------------------------
HEALTH_FOLDER = folder(
    "health",
    [
        make_request(
            name="Liveness — GET /health",
            method="GET",
            path="/health",
            auth=_noauth(),
        ),
        make_request(
            name="Readiness — GET /ready",
            method="GET",
            path="/ready",
            auth=_noauth(),
        ),
    ],
    description="Kubernetes-style liveness/readiness probes. Public, no auth.",
)


# --------------------------------------------------------------------------
# Assemble the collection
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Role-based ordering
# --------------------------------------------------------------------------
# The resource-level folders above (AUTH_FOLDER, STAFF_FOLDER, …)
# are the source of truth for request bodies, scripts, and field
# extraction. Below we re-cut them into per-role folders so a frontend
# dev can open one folder and see every API that role can call.
#
# Roles (top-level folders):
#   1. Auth           — public (login, refresh, OTP, forgot/reset, accept-invitation)
#   2. Admin          — SUPER_ADMIN across all tenants
#   3. Agency Admin   — AGENCY_ADMIN within their tenant
#   4. Staff          — STAFF (field caregivers)
#   5. Patient        — PATIENT (self-service)
#   6. Guardian       — GUARDIAN (linked to children / wards)
#   7. Health         — public probes
#
# Redundancy is intentional and accepted by the product team: the same
# endpoint (e.g. `GET /staff/{id}`) shows up under both Agency Admin
# (read all staff) and Staff (read own profile). Each occurrence is a
# full request so the frontend dev can just open the right folder and
# run them in order.
# --------------------------------------------------------------------------

# `_by_name` re-emits a request from a resource folder by name. We
# rebuild the request entry from a source dict so each role folder
# gets a fresh Auth/header pre-request test that says "this request
# is being run by <Role>".
def _by_name(source: dict[str, Any], role: str) -> dict[str, Any] | None:
    """Return a copy of the request from `source` whose `name` matches.

    `source` is a folder dict (has `item` key). We deep-copy the
    matching request so the role-tagged copy doesn't mutate the
    source folder.
    """
    for req in source.get("item", []):
        if req.get("name") == role:
            return json.loads(json.dumps(req))
    return None


# --------------------------------------------------------------------------
# 1b. Admin cross-tenant people (SUPER_ADMIN) — see
#     qclockcare_backend/src/modules/admin_people/router.py
# --------------------------------------------------------------------------
ADMIN_PEOPLE_FOLDER = folder(
    "admin-people",
    [
        make_request(
            name="List staff across all agencies",
            method="GET",
            path="/admin/people/staff?page=1&page_size=25",
        ),
        make_request(
            name="List patients across all agencies",
            method="GET",
            path="/admin/people/patients?page=1&page_size=25",
        ),
    ],
    description=(
        "Cross-tenant read views for staff + patients (SUPER_ADMIN only). "
        "Use `agency_id`, `status`, and `search` query params to narrow."
    ),
)


# --------------------------------------------------------------------------
# 1. Admin (SUPER_ADMIN) — cross-tenant ops


# (rest of the helper functions unchanged below)
def _requests(source: dict[str, Any], *names: str) -> list[dict[str, Any]]:
    """Resolve a list of request names from a source folder.

    Skips (and warns) any name that doesn't exist — better than
    silently dropping a request.
    """
    found: list[dict[str, Any]] = []
    for n in names:
        r = _by_name(source, n)
        if r is None:
            raise ValueError(
                f"Request '{n}' not found in folder '{source.get('name')}'"
            )
        found.append(r)
    return found


# --------------------------------------------------------------------------
# 1. Admin (SUPER_ADMIN) — cross-tenant ops
# --------------------------------------------------------------------------
ADMIN_FOLDER = folder(
    "Admin",
    [
        # Tenant setup
        *_requests(AGENCIES_FOLDER,
            "List agencies (paginated)",
            "Create agency",
            "Get agency by id",
            "Add agency admin (orphan remediation)",
            "Get deleted agency by id (?include_deleted=true)",
            "Patch agency (rename + status flip)",
            "Soft-delete agency",
            "List programs the agency offers",
        ),
        # Cross-tenant people read views
        *_requests(ADMIN_PEOPLE_FOLDER,
            "List staff across all agencies",
            "List patients across all agencies",
        ),
        # Audit trail — SUPER_ADMIN sees everything across all agencies
        *_requests(AUDIT_LOGS_FOLDER,
            "List audit logs (paginated, filterable)",
            "Get audit log by id",
        ),
        # Locations — admin can write to any agency
        *_requests(LOCATIONS_FOLDER,
            "List locations",
            "Create location",
            "Get location by id",
            "Update location",
            "Archive location (DELETE)",
        ),
        # Notifications — admin can broadcast
        *_requests(NOTIFICATIONS_FOLDER,
            "List my notifications",
            "Get unread badge count",
            "Get notification by id",
            "Mark notification as read",
            "Mark all notifications as read",
            "Send broadcast (AGENCY_ADMIN)",
        ),
    ],
    description=(
        "Cross-tenant admin (SUPER_ADMIN role). Tenant setup, orphan-admin "
        "remediation, agency programs, audit trail, broadcast, and any "
        "location/notification endpoint that SUPER_ADMIN can act on. "
        "Log in as `super@qlockcare.dev` before running these."
    ),
)


# --------------------------------------------------------------------------
# 2. Agency Admin — tenant-level admin
# --------------------------------------------------------------------------
AGENCY_ADMIN_FOLDER = folder(
    "Agency Admin",
    [
        # Staff management
        *_requests(STAFF_FOLDER,
            "List staff (paginated)",
            "Create staff",
            "Get staff by id",
            "Get staff with details",
            "Update staff",
            "Archive staff (DELETE)",
            "List qualifications for staff",
            "Add qualification",
            "Update qualification",
            "Delete qualification",
            "Download qualification file",
            "List availability slots",
            "Add availability slot",
            "Update availability slot",
            "Delete availability slot",
        ),
        # Patient + guardian management
        *_requests(PATIENTS_FOLDER,
            "List patients (paginated)",
            "Create patient",
            "Get patient by id",
            "Get patient with relationships",
            "Update patient",
            "Archive patient (DELETE)",
            "Link guardian to patient",
            "List guardians for patient",
            "Create standalone guardian",
            "List guardians (paginated)",
            "Get guardian by id",
            "Update guardian",
            "Delete guardian",
            "Update relationship",
            "Delete relationship",
        ),
        # Scheduling
        *_requests(APPOINTMENTS_FOLDER,
            "List appointments (paginated)",
            "Create appointment",
            "Get appointment with items",
            "Get appointment by id",
            "Update appointment",
            "Cancel appointment",
            "Transition appointment state",
            "Assign staff to appointment",
            "Mark appointment ready",
            "List activities for appointment",
            "Add activity",
            "Update activity",
            "Delete activity",
        ),
        # Visits — agency admin can do everything
        *_requests(VISITS_FOLDER,
            "Create visit (from appointment)",
            "Get visit by id",
            "Get visit with items",
            "List visits (paginated)",
            "Transition visit state",
            "Confirm visit billing",
            "End visit (record EVV end)",
            "Sign visit (upload signature)",
            "List visit activities",
            "Add visit activity",
            "Update visit activity",
            "Delete visit activity",
            "List visit notes",
            "Add visit note",
            "Start location sharing for visit",
            "Send location ping for visit",
            "Stop location sharing for visit",
        ),
        # Locations — admin of one tenant
        *_requests(LOCATIONS_FOLDER,
            "List locations",
            "Create location",
            "Get location by id",
            "Update location",
            "Archive location (DELETE)",
        ),
        # Notifications
        *_requests(NOTIFICATIONS_FOLDER,
            "List my notifications",
            "Get unread badge count",
            "Get my preferences",
            "Update a preference",
            "Get notification by id",
            "Mark notification as read",
            "Mark all notifications as read",
            "Send broadcast (AGENCY_ADMIN)",
        ),
        # Audit logs — own agency
        *_requests(AUDIT_LOGS_FOLDER,
            "List audit logs (paginated, filterable)",
            "Get audit log by id",
        ),
    ],
    description=(
        "Agency-scoped admin (AGENCY_ADMIN role). Full CRUD on staff, "
        "patients, guardians, appointments, visits, locations, and "
        "notifications within their own agency. Can also broadcast. "
        "Log in as `admin@qlockcare.dev` before running these."
    ),
)


# --------------------------------------------------------------------------
# 3. Staff — field caregivers
# --------------------------------------------------------------------------
STAFF_FOLDER_RF = folder(
    "Staff",
    [
        # Own profile
        *_requests(STAFF_FOLDER,
            "Get staff by id",
            "Get staff with details",
            "List qualifications for staff",
            "Update qualification",
            "Download qualification file",
            "List availability slots",
            "Add availability slot",
            "Update availability slot",
            "Delete availability slot",
        ),
        # Patient list — staff needs to see who they visit
        *_requests(PATIENTS_FOLDER,
            "List patients (paginated)",
            "Get patient by id",
            "List guardians (paginated)",
        ),
        # Appointments — staff sees their assignments
        *_requests(APPOINTMENTS_FOLDER,
            "List appointments (paginated)",
            "Get appointment with items",
            "Get appointment by id",
            "Mark appointment ready",
            "List activities for appointment",
        ),
        # Visits — staff create visit, run lifecycle, sign off
        *_requests(VISITS_FOLDER,
            "Create visit (from appointment)",
            "Get visit by id",
            "Get visit with items",
            "List visits (paginated)",
            "Transition visit state",
            "Confirm visit billing",
            "End visit (record EVV end)",
            "Sign visit (upload signature)",
            "List visit activities",
            "Add visit activity",
            "Update visit activity",
            "Delete visit activity",
            "List visit notes",
            "Add visit note",
            "Start location sharing for visit",
            "Send location ping for visit",
            "Stop location sharing for visit",
        ),
        # Locations — read-only
        *_requests(LOCATIONS_FOLDER,
            "List locations",
            "Get location by id",
        ),
        # Notifications
        *_requests(NOTIFICATIONS_FOLDER,
            "List my notifications",
            "Get unread badge count",
            "Get my preferences",
            "Update a preference",
            "Get notification by id",
            "Mark notification as read",
            "Mark all notifications as read",
        ),
    ],
    description=(
        "Field-caregiver role (STAFF). Own profile + qualifications + "
        "availability, assigned appointments + visits, check-in / "
        "check-out, notes, services, and location reads. "
        "Log in as `staff@qlockcare.dev` before running these."
    ),
)


# --------------------------------------------------------------------------
# 4. Patient — self-service
# --------------------------------------------------------------------------
PATIENT_FOLDER = folder(
    "Patient",
    [
        # Own profile
        *_requests(PATIENTS_FOLDER,
            "Get patient by id",
            "Get patient with relationships",
            "List guardians for patient",
        ),
        # Appointment lifecycle (patient-side actions)
        *_requests(APPOINTMENTS_FOLDER,
            "List appointments (paginated)",
            "Get appointment with items",
            "Get appointment by id",
            "List activities for appointment",
        ),
        # Visits — patient reads visits + signs off via /sign
        *_requests(VISITS_FOLDER,
            "Get visit by id",
            "Get visit with items",
            "List visits (paginated)",
            "Sign visit (upload signature)",
        ),
        # Patient portal — self-service
        *_requests(PORTAL_FOLDER,
            "List my visits (PATIENT)",
            "Get my visit detail (PATIENT)",
            "Report issue on my visit (PATIENT)",
        ),
        # Locations — read-only
        *_requests(LOCATIONS_FOLDER,
            "List locations",
            "Get location by id",
        ),
        # Notifications
        *_requests(NOTIFICATIONS_FOLDER,
            "List my notifications",
            "Get unread badge count",
            "Get my preferences",
            "Update a preference",
            "Get notification by id",
            "Mark notification as read",
            "Mark all notifications as read",
        ),
    ],
    description=(
        "Patient self-service (PATIENT role). Own profile + appointments, "
        "read visits, sign visits via POST /visits/{id}/sign, and the "
        "`/portal/*` self-service endpoints. "
        "Log in as `patient@qlockcare.dev` before running these."
    ),
)


# --------------------------------------------------------------------------
# 5. Guardian — linked to a patient
# --------------------------------------------------------------------------
GUARDIAN_FOLDER = folder(
    "Guardian",
    [
        # Own profile
        *_requests(PATIENTS_FOLDER,
            "Get guardian by id",
        ),
        # Linked patients — guardians see their wards
        *_requests(PATIENTS_FOLDER,
            "Get patient by id",
            "Get patient with relationships",
            "List guardians for patient",
        ),
        # Appointment lifecycle (guardian-side actions)
        *_requests(APPOINTMENTS_FOLDER,
            "List appointments (paginated)",
            "Get appointment with items",
            "Get appointment by id",
            "List activities for appointment",
        ),
        # Visits — guardian reads visits + signs off via /sign
        *_requests(VISITS_FOLDER,
            "Get visit by id",
            "Get visit with items",
            "List visits (paginated)",
            "Sign visit (upload signature)",
        ),
        # Portal — guardian sees same self-service endpoints
        *_requests(PORTAL_FOLDER,
            "List my visits (PATIENT)",
            "Get my visit detail (PATIENT)",
            "Report issue on my visit (PATIENT)",
        ),
        # Locations — read-only
        *_requests(LOCATIONS_FOLDER,
            "List locations",
            "Get location by id",
        ),
        # Notifications
        *_requests(NOTIFICATIONS_FOLDER,
            "List my notifications",
            "Get unread badge count",
            "Get my preferences",
            "Update a preference",
            "Get notification by id",
            "Mark notification as read",
            "Mark all notifications as read",
        ),
    ],
    description=(
        "Guardian role (GUARDIAN). Linked patients, ward's appointments, "
        "read visits, sign visits via POST /visits/{id}/sign, and the "
        "`/portal/*` self-service endpoints. "
        "Log in as a guardian (seed via `patients > Create guardian` "
        "under Agency Admin) before running these."
    ),
)


# --------------------------------------------------------------------------
# Assemble the collection
# --------------------------------------------------------------------------
COLLECTION: dict[str, Any] = {
    "info": {
        "_postman_id": COLLECTION_ID,
        "name": "QlockCare API",
        "description": (
            "End-to-end manual + automated testing for the QlockCare backend.\n\n"
            "**Setup:**\n"
            "1. Import this collection + `environments/Local.postman_environment.json` into Postman.\n"
            "2. Seed a test user: `uv run python scripts/seed_test_user.py`.\n"
            "3. Start the API: `uv run uvicorn src.main:app --port 8001`.\n"
            "4. Open `auth > Login` and click Send. Tokens auto-populate into the env.\n"
            "5. Click into any folder — every request is auto-authenticated.\n\n"
            "**Folders are organized by user role:**\n"
            "- `auth`, `health` — public.\n"
            "- `staff`, `patients`, `appointments`, `visits`, `locations`, `audit-logs`, `notifications > broadcast` — AGENCY_ADMIN.\n"
            "- `agencies` — SUPER_ADMIN or PLATFORM_ADMIN(AGENCIES) for read; SUPER_ADMIN only for write.\n"
            "- `change-password`, `admin-tickets`, `admin-compliance`, `admin-admins` — admin dashboard.\n"
            "- `notifications > list/read/badge` — any authenticated user.\n"
            "- `portal` — PATIENT role only.\n\n"
            "4. Open `Auth > Login` and click Send. Tokens auto-populate into the env.\n"
            "5. Click into any role folder — each request is auto-authenticated.\n\n"
            "**Folders are organized by role**, so a frontend dev can open "
            "one folder and see every API that role can call:\n\n"
            "1. `Auth` — public (login, refresh, logout, me, OTP, forgot/reset, accept-invitation).\n"
            "2. `Admin` — SUPER_ADMIN across all tenants (agencies, audit, locations, broadcasts).\n"
            "3. `Agency Admin` — AGENCY_ADMIN within their tenant (staff, patients, guardians, appointments, visits, locations, notifications, audit).\n"
            "4. `Staff` — STAFF (own profile, qualifications, availability, assigned visits, check-in/out, notes).\n"
            "5. `Patient` — PATIENT (own profile, appointment confirm/reschedule/cancel, visit verify/dispute, `/portal/*`).\n"
            "6. `Guardian` — GUARDIAN (linked patients, ward's appointment lifecycle, visit verify/dispute, `/portal/*`).\n"
            "7. `Health` — public liveness/readiness probes.\n\n"
            "**Redundancy is intentional**: the same endpoint shows up under every "
            "role that can call it. Each occurrence is a full request so the frontend "
            "dev can just open the right folder and run them in order.\n\n"
            "**Smoke-test order for a brand-new tenant:**\n"
            "Auth > Login → Agency Admin > Create agency → Admin > Create staff → "
            "Agency Admin > Create patient → sign out → share the invite link "
            "from the terminal logs (look for `auth.email.dev_invitation_for_test_only`).\n\n"
            "**Role swap:** when switching folders, re-run `Auth > Login` with the "
            "appropriate seeded credentials (`super@`, `admin@`, `staff@`, "
            "`patient@qlockcare.dev`). The collection pre-request script auto-refreshes "
            "expired tokens, so you only need to re-login on role switch.\n\n"
            "**CI:** the same collection runs under Newman on every PR. See `.github/workflows/api-smoke.yml`."
        ),
        "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
    },
    "item": [
        # 1. Auth — public endpoints (no Bearer required).
        AUTH_FOLDER,
        STAFF_FOLDER,
        PATIENTS_FOLDER,
        APPOINTMENTS_FOLDER,
        VISITS_FOLDER,
        PORTAL_FOLDER,
        NOTIFICATIONS_FOLDER,
        LOCATIONS_FOLDER,
        AUDIT_LOGS_FOLDER,
        AGENCIES_FOLDER,
        CHANGE_PASSWORD_FOLDER,
        ADMIN_TICKETS_FOLDER,
        ADMIN_COMPLIANCE_FOLDER,
        ADMIN_ADMINS_FOLDER,
        # 2. Admin (SUPER_ADMIN) — cross-tenant ops.
        ADMIN_FOLDER,
        # 3. Agency Admin (AGENCY_ADMIN) — most-used role.
        AGENCY_ADMIN_FOLDER,
        # 4. Staff (STAFF) — field caregivers.
        STAFF_FOLDER_RF,
        # 5. Patient (PATIENT) — self-service.
        PATIENT_FOLDER,
        # 6. Guardian (GUARDIAN) — linked to patients.
        GUARDIAN_FOLDER,
        # 7. Health — public probes.
        HEALTH_FOLDER,
    ],
    "event": [
        {
            "listen": "prerequest",
            "script": {"type": "text/javascript", "exec": [COLLECTION_PRE_REQUEST]},
        },
    ],
    "variable": [],
    "auth": _bearer_auth(),
}


def main() -> None:
    out = Path(__file__).resolve().parent / "QlockCare_API.postman_collection.json"
    out.write_text(json.dumps(COLLECTION, indent=2) + "\n")

    # Count requests for sanity.
    def _count(items: list[dict[str, Any]]) -> int:
        n = 0
        for it in items:
            if "item" in it:
                n += _count(it["item"])
            else:
                n += 1
        return n

    print(f"Wrote {out}")
    print(f"Top-level folders: {len(COLLECTION['item'])}")
    total = _count(COLLECTION["item"])
    print(f"Total requests (with intentional role duplication): {total}")
    print()
    print("Role order:")
    for f in COLLECTION["item"]:
        print(f"  {f['name']:<14} {_count(f['item']):>3} requests")


if __name__ == "__main__":
    main()
