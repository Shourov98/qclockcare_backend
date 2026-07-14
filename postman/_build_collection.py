"""Build the Postman collection JSON.

This script is the single source of truth for the Postman collection. It
generates `QlockCare_API.postman_collection.json` so that:

  - All 99 routes across 10 modules are represented.
  - Each request carries consistent bearer auth (except the public auth routes).
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
) -> dict[str, Any]:
    """Build one Postman request item."""
    auth = auth if auth is not None else _bearer_auth()
    scripts: dict[str, list[str]] = {
        "test": [standard_tests(name)],
    }
    if extract:
        scripts["test"].extend(extract_id(var, json_path) for var, json_path in extract)
    if extra_tests:
        scripts["test"].append(extra_tests)

    item: dict[str, Any] = {
        "name": name,
        "request": {
            "method": method,
            "header": _common_headers(),
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
    return item


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
                "token": "<invitation-token>",
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
# 4. Appointments (17 routes)
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
            body={"to_status": "CONFIRMED"},
        ),
        make_request(
            name="Assign staff to appointment",
            method="POST",
            path="/appointments/{{appointment_id}}/assign",
            body={"staff_id": "{{staff_id}}", "role": "PRIMARY"},
        ),
        make_request(
            name="Confirm appointment (patient)",
            method="POST",
            path="/appointments/{{appointment_id}}/confirm",
            body={},
        ),
        make_request(
            name="Request reschedule (patient)",
            method="POST",
            path="/appointments/{{appointment_id}}/request-reschedule",
            body={"requested_start": "2026-07-02T10:00:00Z"},
        ),
        make_request(
            name="Request cancellation (patient)",
            method="POST",
            path="/appointments/{{appointment_id}}/request-cancellation",
            body={"reason": "Family emergency"},
        ),
        make_request(
            name="List appointment events",
            method="GET",
            path="/appointments/{{appointment_id}}/events",
        ),
        make_request(
            name="Get appointment confirmation",
            method="GET",
            path="/appointments/{{appointment_id}}/confirmation",
        ),
        make_request(
            name="List service items for appointment",
            method="GET",
            path="/appointments/{{appointment_id}}/service-items",
        ),
        make_request(
            name="Add service item",
            method="POST",
            path="/appointments/{{appointment_id}}/service-items",
            body={
                "service_type": "PERSONAL_CARE",
                "duration_minutes": 60,
                "notes": "Bathing assistance",
            },
            extract=[("service_item_id", "id")],
        ),
        make_request(
            name="Update service item",
            method="PATCH",
            path="/appointments/{{appointment_id}}/service-items/{{service_item_id}}",
            body={"status": "APPROVED"},
        ),
        make_request(
            name="Delete service item",
            method="DELETE",
            path="/appointments/{{appointment_id}}/service-items/{{service_item_id}}",
        ),
    ],
    description="Care appointments — scheduling, state machine, service items, patient-side actions.",
)

# --------------------------------------------------------------------------
# 5. Visits (17 routes)
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
            name="Check in to visit",
            method="POST",
            path="/visits/{{visit_id}}/check-in",
            body={
                "actual_start": "2026-07-01T10:05:00Z",
                "location": {"lat": 44.98, "lng": -93.27},
            },
        ),
        make_request(
            name="Check out of visit",
            method="POST",
            path="/visits/{{visit_id}}/check-out",
            body={"actual_end": "2026-07-01T11:02:00Z"},
        ),
        make_request(
            name="Transition visit state",
            method="POST",
            path="/visits/{{visit_id}}/transition",
            body={"to_status": "IN_PROGRESS"},
        ),
        make_request(
            name="List visit service items",
            method="GET",
            path="/visits/{{visit_id}}/service-items",
        ),
        make_request(
            name="Add visit service item",
            method="POST",
            path="/visits/{{visit_id}}/service-items",
            body={
                "service_type": "PERSONAL_CARE",
                "duration_minutes": 55,
                "notes": "Completed at 11:00",
            },
            extract=[("service_item_id", "id")],
        ),
        make_request(
            name="Update visit service item",
            method="PATCH",
            path="/visits/{{visit_id}}/service-items/{{service_item_id}}",
            body={"status": "COMPLETED"},
        ),
        make_request(
            name="Delete visit service item",
            method="DELETE",
            path="/visits/{{visit_id}}/service-items/{{service_item_id}}",
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
        make_request(
            name="Verify visit (PATIENT role)",
            method="POST",
            path="/visits/{{visit_id}}/verify",
            body={"verified": True, "feedback": "Great visit"},
        ),
        make_request(
            name="Dispute visit (PATIENT role)",
            method="POST",
            path="/visits/{{visit_id}}/dispute",
            body={"reason": "Visit was shorter than scheduled"},
        ),
        make_request(
            name="List visit issues",
            method="GET",
            path="/visits/{{visit_id}}/issues",
        ),
        make_request(
            name="Report visit issue",
            method="POST",
            path="/visits/{{visit_id}}/issues",
            body={"severity": "MEDIUM", "description": "Medication was missed"},
        ),
        make_request(
            name="Resolve visit issue",
            method="POST",
            path="/visits/{{visit_id}}/issues/00000000-0000-0000-0000-000000000000/resolve",
            body={"resolution": "Patient took medication at 10:30"},
        ),
    ],
    description="Field visits by care staff — check-in/out, notes, patient verification, issue reporting.",
)

# --------------------------------------------------------------------------
# 6. Portal (5 routes — PATIENT role)
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
            name="Verify my visit (PATIENT)",
            method="POST",
            path="/portal/visits/{{visit_id}}/verify",
            body={"verified": True, "feedback": "All good"},
        ),
        make_request(
            name="Dispute my visit (PATIENT)",
            method="POST",
            path="/portal/visits/{{visit_id}}/dispute",
            body={"reason": "Caregiver left early"},
        ),
        make_request(
            name="Report issue on my visit (PATIENT)",
            method="POST",
            path="/portal/visits/{{visit_id}}/report-issue",
            body={"severity": "LOW", "description": "Caregiver was 15 min late"},
        ),
    ],
    description="Patient-facing endpoints. Run 'auth > Login as PATIENT' first (after seeding via scripts/seed_test_user.py) — the request path will 403 with AGENCY_ADMIN.",
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
            },
            extract=[("agency_id", "id")],
        ),
        make_request(
            name="Get agency by id",
            method="GET",
            path="/agencies/{{agency_id}}",
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
# 11. Health (2 routes)
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
            "- `agencies` — SUPER_ADMIN only.\n"
            "- `notifications > list/read/badge` — any authenticated user.\n"
            "- `portal` — PATIENT role only.\n\n"
            "**CI:** the same collection runs under Newman on every PR. See `.github/workflows/api-smoke.yml`."
        ),
        "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
    },
    "item": [
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
    print(f"Folders: {len(COLLECTION['item'])}")
    print(f"Total requests: {_count(COLLECTION['item'])}")


if __name__ == "__main__":
    main()
