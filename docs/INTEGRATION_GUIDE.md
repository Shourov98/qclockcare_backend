# QlockCare Backend — Mobile App Integration Guide

> A practical, hand-hold guide for an app developer integrating a
> native (iOS / Android / React Native / Flutter) client with the
> QlockCare backend. Covers the three end-user roles that need a
> mobile experience: **PATIENT**, **GUARDIAN**, and **AGENCY STAFF**
> (caregiver).
>
> Last updated: 2026-08-18
> Base URL (dev): `http://localhost:8000`
> Base URL (prod): `https://api.qlockcare.dev` *(example)*
> OpenAPI: `/docs` (Swagger UI) and `/openapi.json` on a running server.

---

## Table of contents

1. [Before you start](#1-before-you-start)
2. [Roles, base URLs, environment flags](#2-roles-base-urls-environment-flags)
3. [HTTP / JSON / envelope conventions](#3-http--json--envelope-conventions)
4. [Authentication: login, refresh, CSRF, logout](#4-authentication-login-refresh-csrf-logout)
5. [Role-based navigation decision tree](#5-role-based-navigation-decision-tree)
6. [Common recipes](#6-common-recipes)
   - 6.1. Fetching the current user (`/auth/me`)
   - 6.2. Polling unread notifications (`/notifications/badge`)
   - 6.3. Uploading GPS pings during a visit
   - 6.4. Streaming a Claude-narrative report (SSE)
7. [Role-specific endpoints](#7-role-specific-endpoints)
   - 7.1. PATIENT (`/portal/visits`)
   - 7.2. GUARDIAN (`/portal/visits`, multi-patient)
   - 7.3. AGENCY STAFF (visits + appointments + notes)
8. [Error reference](#8-error-reference)
9. [Rate limits, timeouts, retries](#9-rate-limits-timeouts-retries)
10. [Storage & security checklist](#10-storage--security-checklist)
11. [Testing without a backend](#11-testing-without-a-backend)
12. [Appendix: full sample requests](#12-appendix-full-sample-requests)

---

## 1. Before you start

You need:

- **API base URL.** Dev: `http://localhost:8000`. Production: provided by the platform team (every environment has a separate URL).
- **A test user.** Ask the platform team for a sandbox agency and a couple of patient / caregiver credentials. **Don't ship against dev URLs in production.**
- **An HTTP client that handles** bearer tokens, custom headers, and at least 2xx / 4xx / 5xx semantics. Swift `URLSession`, Kotlin `OkHttp`, `axios`, `dio` all work.
- **A way to test SSE** (Server-Sent Events). See §6.4.

You do **not** need to install any SDK — the backend is plain REST + SSE.

---

## 2. Roles, base URLs, environment flags

The backend recognizes five user roles. This guide covers three:

| Role | What they do on a mobile device |
|---|---|
| **PATIENT** | View their upcoming and past visits, confirm (verify) visits, dispute, report issues. |
| **GUARDIAN** | Same as patient, but acts for one or more patients. |
| **AGENCY STAFF** (caregiver) | See their schedule, check in / out of a visit, share live GPS, write visit notes, report issues. |

The other two (`AGENCY_ADMIN`, `SUPER_ADMIN`) are managed by the web apps
(`farhan-salad-website`, `farhan-salad-admin`) and aren't covered here.

Every backend response carries the role on the user object returned by
`/auth/me`:

```json
{
  "id": "9c7f6…",
  "email": "patient@example.com",
  "full_name": "Maria K.",
  "status": "ACTIVE",
  "email_verified": true,
  "agency_id": "5b1c…",
  "role": "PATIENT"
}
```

The `role` field drives which screens and endpoints the app exposes.
See §5 for the decision tree.

**Feature flags.** Some endpoints return `503` when a feature is
disabled server-side. The two you'll hit on mobile:

- `FEATURE_BILLING_ENABLED` — controls `/billing/*` and
  `/agencies/{id}/billing/*` (Stripe). Usually off in dev.
- `FEATURE_REPORTS_AI_NARRATIVE` — controls `/reports/{type}/stream`.
  Returns `503` with `code: SERVICE_UNAVAILABLE` when off.

You'll see the `503` envelope in §3; treat it as "feature unavailable,
don't crash."

---

## 3. HTTP / JSON / envelope conventions

### Content types
- Request bodies: `Content-Type: application/json` (UTF-8).
- Responses: `application/json`.
- Streaming (Reports): `text/event-stream` — see §6.4.

### Headers you MUST send
- `Authorization: Bearer <access_token>` — on every request after login.
  Skip for `/auth/login`, `/auth/refresh`, `/auth/forgot-password`,
  `/auth/reset-password`, `/auth/accept-invitation`, `/auth/verify-email`,
  `/auth/resend-otp`, and the Stripe webhook (server-only).
- `X-Request-ID: <uuid>` — optional but recommended; the backend echoes
  it back on the response. Useful when filing support tickets.

### Headers you SHOULD send
- `X-CSRF-Token: <csrf_cookie_value>` — on every non-GET request that
  carries an `access_token`. The backend sets a `qc_csrf` cookie on
  login; you copy its value into this header. **For native apps,
  read the cookie from the response and store it alongside the
  access token.** See §4.4.

### Standard response envelope (success)
Most list / detail endpoints return the resource directly:

```json
{ "id": "...", "created_at": "...", ... }
```

Paginated lists use an offset envelope:

```json
{
  "data": [ { "id": "...", ... }, ... ],
  "page": 1,
  "page_size": 20,
  "total": 123,
  "total_pages": 7
}
```

Cursor-paginated lists (notably `/notifications`) use:

```json
{
  "data": [ ... ],
  "next_cursor": "eyJjcmVhdGVkX2F0Ijoi...",   // pass back as ?cursor=
  "unread_count": 4
}
```

### Standard response envelope (error)
Every non-2xx response uses one shape:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Invalid email format",
    "details": { "field": "email" }
  }
}
```

(See §8 for the full `code` table.)

`null` body with a status code is also valid for some `204`/`3xx`
responses (e.g. successful `DELETE`).

---

## 4. Authentication: login, refresh, CSRF, logout

### 4.1 Login

```
POST /auth/login
Content-Type: application/json
```

Body:
```json
{ "email": "patient@example.com", "password": "••••••••" }
```

Response (200):
```json
{
  "access_token": "eyJhbGciOi...",
  "refresh_token": "eyJhbGciOi...",
  "token_type": "bearer",
  "expires_in": 900,
  "user": {
    "id": "9c7f6…",
    "email": "patient@example.com",
    "full_name": "Maria K.",
    "status": "ACTIVE",
    "email_verified": true,
    "agency_id": "5b1c…",
    "role": "PATIENT"
  }
}
```

`expires_in` is in **seconds** (typically 900 = 15 minutes for the
access token; the refresh token is longer-lived, usually 30 days).

**Store on the device:**
- `access_token` — for the `Authorization` header.
- `refresh_token` — for getting a new access token.
- `user` — drive role-based navigation immediately on login.

### 4.2 Refresh (silent re-auth)

```
POST /auth/refresh
Content-Type: application/json
```

Body:
```json
{ "refresh_token": "eyJhbGciOi..." }
```

Returns the same shape as login. **Schedule this ~60 seconds before
`expires_in` elapses**, or transparently on any 401 (see §9).

If refresh returns 401 (refresh token revoked or expired), drop
everything and bounce the user to the sign-in screen.

### 4.3 Logout

```
POST /auth/logout
Authorization: Bearer <access_token>
X-CSRF-Token: <csrf>
```

The backend blacklists the refresh token. **Always call logout before
clearing local state**, even on app uninstall — it prevents a
stolen-token window.

### 4.4 CSRF — read this even if you're not building a website

The backend sets a `qc_csrf` cookie on the very first response from
`/auth/login`. Your HTTP client must:

1. **Persist cookies** (so the `qc_csrf` cookie is retained across
   requests). URLSession's `HTTPCookieStorage.shared` does this;
   OkHttp's `CookieJar` does this; `axios` needs the `withCredentials:
   true` flag plus `cookieJar` plumbing.
2. On every non-GET request, copy the `qc_csrf` cookie value into the
   `X-CSRF-Token` header.

Example with `curl` (the backend dev team uses this in tests):

```bash
curl -c cookies.txt -X POST http://localhost:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"patient@example.com","password":"••••••••"}'

curl -b cookies.txt http://localhost:8000/auth/me \
  -H "Authorization: Bearer $(jq -r .access_token < login.json)"
```

If you skip CSRF, the backend returns `403` with `code: CSRF_TOKEN_MISSING`
on writes. Login still works (it's exempt), but every other mutation
will be rejected.

### 4.5 Forgot / reset password

```
POST /auth/forgot-password        { "email": "..." }      # returns 204
POST /auth/reset-password         { "token": "...", "new_password": "..." }   # 204
POST /auth/verify-email           { "otp": "123456" }
POST /auth/resend-otp             {}
POST /auth/accept-invitation      { "token": "...", "full_name": "...", "password": "..." }
```

All four return `204` on success. The reset token is sent via email
(link in dev: check MailHog at `localhost:8025`).

---

## 5. Role-based navigation decision tree

Immediately after login, you have the `user.role`. Branch on it:

```
user.role
├── "PATIENT"        → Patient tab bar:
│                       • Upcoming visits
│                       • Past visits
│                       • Verify / dispute
│                       • Profile (settings + logout)
│
├── "GUARDIAN"       → Same as PATIENT, but the home screen shows a
│                       picker of linked patients. Each screen filters
│                       data by the chosen patient_id. (See §7.2.)
│
└── "AGENCY STAFF"   → Caregiver tab bar:
                        • Today's schedule
                        • Active visit (clock in / out + GPS)
                        • Notes & issues
                        • Profile (settings + logout)
```

You don't need a separate login flow per role — the same `/auth/login`
returns any of these roles. Just branch on the `role` field.

`AGENCY_ADMIN` and `SUPER_ADMIN` log in to the web apps, not this
mobile client.

---

## 6. Common recipes

### 6.1 Fetching the current user

```
GET /auth/me
Authorization: Bearer <access_token>
```

Returns the `user` object. Call this on app launch to verify the
stored token is still valid and to refresh user metadata.

```bash
curl http://localhost:8000/auth/me \
  -H "Authorization: Bearer eyJhbGciOi..."
```

```json
{
  "id": "9c7f6…",
  "email": "patient@example.com",
  "full_name": "Maria K.",
  "status": "ACTIVE",
  "email_verified": true,
  "agency_id": "5b1c…",
  "role": "PATIENT"
}
```

### 6.2 Polling unread notifications

```
GET /notifications/badge
Authorization: Bearer <access_token>
```

Response:
```json
{ "unread_count": 4 }
```

**Recommendation:** poll every 60 seconds while in foreground, pause on
background. The full list lives at:

```
GET /notifications?limit=20&unread_only=false&cursor=<next_cursor>
```

To mark a notification as read:

```
PATCH /notifications/{id}/read
Authorization: Bearer <access_token>
X-CSRF-Token: <csrf>
```

To mark all as read:

```
POST /notifications/read-all
Authorization: Bearer <access_token>
X-CSRF-Token: <csrf>
```

Returns `{ "marked": 12 }`.

### 6.3 Uploading GPS pings during a visit

When a caregiver opts in to share live location:

```
POST /visits/{visit_id}/start-location-sharing
Authorization: Bearer <access_token>
X-CSRF-Token: <csrf>
Content-Type: application/json
```

Body (optional seed):
```json
{
  "initial_lat": 44.9778,
  "initial_lng": -93.2650,
  "initial_accuracy_m": 12.5
}
```

Then every ~15 seconds while the visit is in progress:

```
POST /visits/{visit_id}/location-ping
Authorization: Bearer <access_token>
X-CSRF-Token: <csrf>
Content-Type: application/json
```

Body:
```json
{
  "lat": 44.9778,
  "lng": -93.2650,
  "accuracy_m": 12.5,
  "device_id": "ios-abc-123"
}
```

Response is the full `VisitResponse` (see §7.3). The backend stores
only the most recent ping (no history table by design).

When the visit ends:

```
POST /visits/{visit_id}/stop-location-sharing
```

### 6.4 Streaming a Claude-narrative report (SSE)

The Reports module streams a Claude-generated narrative as Server-Sent
Events. This is the only SSE endpoint in the API surface today.

```
POST /reports/{report_type}/stream
Authorization: Bearer <access_token>
X-CSRF-Token: <csrf>
Content-Type: application/json
```

`report_type` is one of:
`visit_summary`, `billing`, `compliance`, `client`, `staff`, `evv`,
`group_home`, `audit_readiness`, `custom`, `ai_insights`.

Body:
```json
{ "params": { "date_from": "2026-07-01", "date_to": "2026-07-31" } }
```

The response is `text/event-stream`. Each frame is:

```
data: {"kind": "run_meta",   "run_id": "..."}\n\n
data: {"kind": "delta",       "delta": "The "}\n\n
data: {"kind": "delta",       "delta": "agency "}\n\n
data: {"kind": "delta",       "delta": "had "}\n\n
...
data: {"kind": "final",       "total_tokens": 782, "cost_usd": 0.0143}\n\n
```

The client concatenates every `delta` to render the narrative
incrementally. Errors come as:

```
data: {"kind": "error", "error": "Claude request timed out"}\n\n
```

**Caveats:**
- The connection stays open for ~30–90 seconds (Claude generation).
- The server can close the connection mid-stream. Treat a close-without-final
  as a failure and offer the user a "retry" button.
- No reconnect logic in the current backend; client should not retry on its own.
- **The Reports endpoint is for STAFF/AGENCY_ADMIN; it's not relevant
  for the PATIENT/GUARDIAN mobile app** — included here so you know it exists.

---

## 7. Role-specific endpoints

### 7.1 PATIENT

All patient endpoints live under `/portal/visits`. The backend rejects
calls from non-patient/guardian roles.

#### List my visits

```
GET /portal/visits?limit=20&status=COMPLETED
Authorization: Bearer <access_token>
```

`status` filter values: `SCHEDULED`, `CHECKED_IN`, `IN_PROGRESS`,
`COMPLETED`, `CHECKED_OUT`, `VERIFIED`, `DISPUTED`, `MISSED`, `CANCELLED`.
Returns at most `limit` (default 20, max 100).

Response:
```json
[
  {
    "id": "visit-uuid",
    "appointment_id": "appt-uuid",
    "status": "COMPLETED",
    "check_in_time": "2026-08-12T14:02:00Z",
    "check_out_time": "2026-08-12T15:30:00Z",
    "duration_seconds": 5280
  },
  ...
]
```

#### Get one visit

```
GET /portal/visits/{visit_id}
Authorization: Bearer <access_token>
```

Returns the full visit plus service items, verification, and issues
(everything the patient is allowed to see — not staff notes).

#### Confirm (verify) a visit

```
POST /portal/visits/{visit_id}/verify
Authorization: Bearer <access_token>
X-CSRF-Token: <csrf>
Content-Type: application/json
```

Body:
```json
{
  "status": "VERIFIED",
  "note": "Caregiver arrived on time and was very professional."
}
```

`status` may be `VERIFIED` or `DISPUTED`. The backend emits a
notification to the agency admin.

#### Dispute a visit

```
POST /portal/visits/{visit_id}/dispute
Authorization: Bearer <access_token>
X-CSRF-Token: <csrf>
Content-Type: application/json
```

Body:
```json
{
  "dispute_reason_code": "DID_NOT_OCCUR",
  "note": "I was in the hospital that day and no one came."
}
```

`dispute_reason_code` is one of: `DID_NOT_OCCUR`, `INCORRECT_DURATION`,
,
 `INCORRECT_SERVICES`, `CAREGIVER_LATE`, `OTHER`.

#### Report an issue on a visit

```
POST /portal/visits/{visit_id}/issues
Authorization: Bearer <access_token>
X-CSRF-Token: <csrf>
Content-Type: application/json
```

Body:
```json
{
  "reason_code": "CAREGIVER_LATE",
  "description": "Caregiver arrived 45 minutes late."
}
```

Returns the created issue. The patient can re-list issues via
`GET /portal/visits/{visit_id}/issues` (same endpoint family as staff,
but RLS scopes the rows to the caller).

### 7.2 GUARDIAN

A guardian is the same backend role as patient but their user is
linked to **one or more** patient rows. To know which patient you're
acting for, the agency admin's onboarding flow surfaces the link list
to the guardian during invitation, and **the mobile app should persist
the linked patient IDs locally on first launch** (the backend doesn't
expose a single "list my linked patients" endpoint yet — see §13).

Once you've selected a patient in the UI, all `/portal/visits/*` calls
filter by `patient_id` automatically — **but you must pass `patient_id`
as a query param when listing**:

```
GET /portal/visits?patient_id=<linked_patient_uuid>&limit=20
Authorization: Bearer <access_token>
```

The backend enforces that the guardian is linked to that patient. If
the link doesn't exist, you get a `404` (not 403 — the backend avoids
leaking visit existence).

#### Discovering the guardian's own profile

When the guardian signs in for the first time, look up their own
guardian profile to confirm linkage status:

```
GET /guardians/{guardian_id}
Authorization: Bearer <access_token>
```

The app should know its own `guardian_id` from the agency's onboarding
payload (the API doesn't expose it via `/auth/me` yet). If the local
copy is missing, the app can call the agency admin to recover it.

#### Listing a patient's guardians

Useful for the "linked accounts" settings page:

```
GET /patients/{patient_id}/guardians
Authorization: Bearer <access_token>
```

Returns every patient↔guardian relationship (active + expired). The
guardian app should be careful to render only **other** linked
guardians, not itself, to avoid confusion.

#### Linking a new patient to a guardian

This is done by the agency admin, **not** by the guardian from their
phone. The link is created via the admin's web app, which calls:

```
POST /patients/{patient_id}/guardians
```

with body `{ "guardian_user_id": "...", "relationship": "DAUGHTER" }`.
The mobile guardian app does not write to this endpoint.

#### Known gap

The backend doesn't expose a single endpoint that, given the
caller's user_id, returns the list of patients they're linked to.
Two options until that endpoint ships:

- **App keeps the list locally.** The agency admin's onboarding flow
  passes the linked patient IDs to the guardian's device out-of-band
  (e.g. via a deep link from the email invitation), and the app stores
  them. Trade-off: stale data if a link is added later.
- **App calls `/patients/{patient_id}/guardians` once per known
  patient.** Practical only if the guardian is linked to a small
  number of patients. The "small list" approach.

A `GET /guardians/me/patients` endpoint is on the roadmap.

### 7.3 AGENCY STAFF (caregiver)

Staff endpoints cover scheduling, the live visit, and notes. All
endpoints are scoped to the staff member by RLS — staff only see what
they're assigned to.

#### List my appointments (schedule)

```
GET /appointments?staff_id=<self_id>&date_from=...&date_to=...
Authorization: Bearer <access_token>
```

Response (paginated):
```json
{
  "data": [
    {
      "id": "appt-uuid",
      "patient_id": "...",
      "scheduled_start": "2026-08-18T14:00:00Z",
      "scheduled_end":   "2026-08-18T15:00:00Z",
      "status": "SCHEDULED",
      "service_items": [
        { "id": "...", "service_code": "PERSONAL_CARE", "duration_minutes": 60 }
      ]
    }
  ],
  "page": 1, "page_size": 20, "total": 14, "total_pages": 1
}
```

#### Get one appointment (with service items)

```
GET /appointments/{id}/with-items
Authorization: Bearer <access_token>
```

#### List visits assigned to me (live monitor)

```
GET /visits?staff_id=<self_id>&status=IN_PROGRESS
Authorization: Bearer <access_token>
```

Filters: `appointment_id`, `patient_id`, `status`, `sharing_id` (true =
visit is broadcasting live GPS). Combine as needed.

#### Check in to a visit

Either create the visit on check-in:

```
POST /visits
Authorization: Bearer <access_token>
X-CSRF-Token: <csrf>
Content-Type: application/json
```

Body:
```json
{
  "appointment_id": "appt-uuid",
  "check_in_lat": 44.9778,
  "check_in_lng": -93.2650,
  "check_in_accuracy_m": 12.5,
  "check_in_device_id": "ios-abc-123",
  "check_in_address_match": true,
  "check_in_distance_from_location_m": 8
}
```

Or, if the visit row was pre-created by the scheduler, just patch
the check-in fields:

```
PATCH /visits/{id}/check-in
Authorization: Bearer <access_token>
X-CSRF-Token: <csrf>
Content-Type: application/json
```

Body:
```json
{
  "check_in_lat": 44.9778,
  "check_in_lng": -93.2650,
  "check_in_accuracy_m": 12.5,
  "check_in_device_id": "ios-abc-123"
}
```

Both responses are the full `VisitResponse` with `status: "CHECKED_IN"`.

#### Check out of a visit

```
PATCH /visits/{id}/check-out
Authorization: Bearer <access_token>
X-CSRF-Token: <csrf>
Content-Type: application/json
```

Body:
```json
{
  "check_out_lat": 44.9780,
  "check_out_lng": -93.2645,
  "check_out_accuracy_m": 9.0,
  "note": "Patient in good spirits; lunch prepared."
}
```

The backend stamps `status = "CHECKED_OUT"` and computes
`duration_seconds` automatically. Don't compute it client-side.

#### Add a note to a visit

```
POST /visits/{id}/notes
Authorization: Bearer <access_token>
X-CSRF-Token: <csrf>
Content-Type: application/json
```

Body:
```json
{ "body": "Patient refused breakfast; offered snack at 10:30 instead." }
```

To list notes:
```
GET /visits/{id}/notes
```

#### Report an issue on a visit

```
POST /visits/{id}/issues
Authorization: Bearer <access_token>
X-CSRF-Token: <csrf>
Content-Type: application/json
```

Body:
```json
{
  "reason_code": "CLIENT_REFUSED_SERVICE",
  "description": "Patient declined bathing assistance."
}
```

Reason codes: see backend `VisitIssue.reason_code` enum.
Resolution is admin-only (`PATCH /visits/{id}/issues/{issue_id}/resolve`).

#### Get visit detail (incl. service items, notes, verification, issues)

```
GET /visits/{id}/with-items
Authorization: Bearer <access_token>
```

This is the endpoint the staff app's "visit detail" screen calls.

---

## 8. Error reference

The backend returns `4xx` / `5xx` with this envelope:

```json
{
  "error": {
    "code": "STRING_CODE",
    "message": "Human-readable explanation.",
    "details": { /* optional, type varies */ }
  }
}
```

| Status | `code` | What it means | What the app should do |
|---|---|---|---|
| 400 | `BAD_REQUEST` | Malformed JSON, missing query param. | Show a generic "try again" message. |
| 401 | `UNAUTHENTICATED` | No/invalid bearer token. | Run silent refresh; on failure, bounce to sign-in. |
| 401 | `TOKEN_EXPIRED` | Access token past `expires_in`. | Trigger refresh. |
| 401 | `REFRESH_TOKEN_INVALID` | Refresh token revoked/expired. | Force sign-out (delete local tokens). |
| 403 | `FORBIDDEN` | Caller's role can't access this endpoint. | Show a permission screen, not an error toast. |
| 403 | `CSRF_TOKEN_MISSING` | `X-CSRF-Token` header absent on a write. | Read cookie jar, retry once. |
| 403 | `CSRF_TOKEN_INVALID` | CSRF token doesn't match the cookie. | Same as above. |
| 403 | `CROSS_AGENCY_ACCESS_DENIED` | The caller is trying to read another agency's record. | Treat as not-found (don't leak existence). |
| 404 | `NOT_FOUND` | The resource doesn't exist or the caller can't see it. | Show a friendly "not available" screen. |
| 409 | `CONFLICT` | E.g. agency already exists, status transition not allowed. | Show the `message` field directly. |
| 422 | `VALIDATION_ERROR` | Field-level validation. | Map `details` to form fields. |
| 429 | `RATE_LIMITED` | Too many requests in a window. | Read `Retry-After` header (seconds). |
| 500 | `INTERNAL_ERROR` | Unexpected backend fault. | Generic error; capture `X-Request-ID` and report. |
| 503 | `SERVICE_UNAVAILABLE` | Feature flag off (billing/AI off) or Stripe misconfigured. | Hide the feature; don't crash. |

Every error response **also** carries `X-Request-ID`. When filing a
bug, include it — it ties the report to a backend log entry.

---

## 9. Rate limits, timeouts, retries

**Rate limits.** The backend uses `slowapi` per-endpoint. Examples:

- Login: 5 / minute per IP.
- `/auth/refresh`: 30 / minute per IP.
- AI narrative (`/reports/{type}/stream`): 5 / minute per user.
- All other reads: 60 / minute per user.
- All other writes: 30 / minute per user.

When you exceed a limit, the response is `429` with `Retry-After`.
Back off exponentially; never hammer.

**Timeouts.** Use these per-request timeouts (generous):

- Reads: 10 s
- Writes: 15 s
- `/reports/{type}/stream` (SSE): 120 s (the generation can itself
  take 60–90 s; treat the connection as long-lived).

**Retries.**
- Retry once on `5xx` (except `503 SERVICE_UNAVAILABLE`).
- Do **not** retry `4xx` — except `401 UNAUTHENTICATED` and `403 CSRF_TOKEN_*`,
  which should trigger a refresh + retry.
- Idempotency keys: not currently supported. Don't retry `POST` /
  `PATCH` blindly; surface the error to the user.

---

## 10. Storage & security checklist

- **Tokens in Keychain (iOS) / Keystore (Android).** Never `UserDefaults`
  / `SharedPreferences`. The backend will reject tokens if a forensic
  anomaly is detected.
- **Pin / biometric gate on app launch.** Even if a device is lost,
  the stored token alone shouldn't grant access.
- **Strip tokens from logs.** Add a global `URLProtocol` / `OkHttp`
  interceptor that redacts the `Authorization` header.
- **TLS only in production.** Plain HTTP is acceptable in dev (the
  backend accepts it on `localhost`), but production builds must
  enforce HTTPS — reject `http://` URLs at app start.
- **Certificate pinning** is optional. The platform team uses Let's
  Encrypt; pinning is recommended for high-risk builds (institutional
  care agencies).
- **GDPR / HIPAA.** Don't persist visit notes or GPS history client-side.
  Always re-fetch; never cache PII beyond the current screen.
- **Foreground-only GPS.** Stop the location-ping `setInterval` when
  the app backgrounds. The backend only persists the **most recent**
  ping per visit, but the OS can keep emitting pings you don't need.

---

## 11. Testing without a backend

Two options:

**A. Use the staging environment.** Ask the platform team for
`https://staging.api.qlockcare.dev` credentials. Has the full backend
running but with anonymized data.

**B. Run the backend locally.** The backend uses Docker Compose for
dev dependencies (Postgres, MailHog). `cd qclockcare_backend && make dev`
boots it; `http://localhost:8000/docs` shows the live OpenAPI.

You'll also want **MailHog** at `localhost:8025` to read invitation /
password-reset emails during testing.

---

## 12. Appendix: full sample requests

The following are copy-pasteable. Replace `$URL` with your base URL
and `$TOKEN` with the value of `access_token` from login.

### PATIENT — fetch my visits

```bash
curl -s $URL/portal/visits?limit=20 \
  -H "Authorization: Bearer $TOKEN" | jq .
```

### PATIENT — verify a visit

```bash
curl -s -X POST $URL/portal/visits/$VISIT_ID/verify \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-CSRF-Token: $CSRF" \
  -H "Content-Type: application/json" \
  -d '{"status":"VERIFIED","note":"All good."}' | jq .
```

### GUARDIAN — list linked patients

```bash
curl -s $URL/patients/me/guardian-links \
  -H "Authorization: Bearer $TOKEN" | jq .
```

### GUARDIAN — list visits for a chosen patient

```bash
curl -s "$URL/portal/visits?patient_id=$PATIENT_ID&limit=20" \
  -H "Authorization: Bearer $TOKEN" | jq .
```

### STAFF — list my schedule for today

```bash
TODAY=$(date -u +%Y-%m-%d)
curl -s "$URL/appointments?staff_id=$STAFF_ID&date_from=$TODAY&date_to=$TODAY" \
  -H "Authorization: Bearer $TOKEN" | jq .
```

### STAFF — check in (create visit + check-in fields)

```bash
curl -s -X POST $URL/visits \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-CSRF-Token: $CSRF" \
  -H "Content-Type: application/json" \
  -d "{
    \"appointment_id\": \"$APPT_ID\",
    \"check_in_lat\": 44.9778,
    \"check_in_lng\": -93.2650,
    \"check_in_accuracy_m\": 12.5,
    \"check_in_device_id\": \"ios-abc-123\"
  }" | jq .
```

### STAFF — start sharing live GPS

```bash
curl -s -X POST $URL/visits/$VISIT_ID/start-location-sharing \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-CSRF-Token: $CSRF" \
  -H "Content-Type: application/json" \
  -d '{}' | jq .
```

### STAFF — ping GPS (every ~15 s)

```bash
curl -s -X POST $URL/visits/$VISIT_ID/location-ping \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-CSRF-Token: $CSRF" \
  -H "Content-Type: application/json" \
  -d '{"lat":44.9778,"lng":-93.2650,"accuracy_m":12.5,"device_id":"ios-abc-123"}' | jq .
```

### STAFF — check out

```bash
curl -s -X PATCH $URL/visits/$VISIT_ID/check-out \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-CSRF-Token: $CSRF" \
  -H "Content-Type: application/json" \
  -d '{"check_out_lat":44.9780,"check_out_lng":-93.2645,"check_out_accuracy_m":9.0,"note":"Visit complete."}' | jq .
```

### STAFF — add a note

```bash
curl -s -X POST $URL/visits/$VISIT_ID/notes \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-CSRF-Token: $CSRF" \
  -H "Content-Type: application/json" \
  -d '{"body":"Patient refused breakfast; offered snack at 10:30 instead."}' | jq .
```

---

**Need something not covered here?** Two sources of truth:

1. The **OpenAPI page** at `$URL/docs` — auto-generated, always current.
2. The **Postman collection** at `qclockcare_backend/postman/QlockCare_API.postman_collection.json` — every endpoint, pre-filled with `$access_token`, `$csrf`, and PM tests.

If a discrepancy appears between this guide and either of the above,
the Postman collection is the canonical spec. File a PR against
`docs/INTEGRATION_GUIDE.md` to keep it in sync.