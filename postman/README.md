# QlockCare API — Postman Collection

Manual exploration + CI smoke testing for the QlockCare backend.

The collection covers **all 99 routes** across 10 modules. Each request
has standard test scripts (status code, envelope shape, X-Request-ID
round-trip) and the requests that produce IDs auto-extract them into
the active environment, so chained requests (create → read → update →
delete) work without copy-pasting UUIDs.

---

## Quick start (5 minutes)

### 1. Install Postman + Newman (one-time)

```bash
# Postman desktop: https://www.postman.com/downloads/
# Newman (CI + scripted runs):
npm install -g newman newman-reporter-html
```

### 2. Seed the dev database

The collection expects a known set of users to log in as:

```bash
uv run python scripts/seed_test_user.py
```

This creates (idempotently):

| Role | Email | Password |
|---|---|---|
| SUPER_ADMIN | `super@qlockcare.dev` | `SuperDevPass123!` |
| AGENCY_ADMIN | `admin@qlockcare.dev` | `AdminDevPass123!` |
| STAFF | `staff@qlockcare.dev` | `StaffDevPass123!` |
| PATIENT | `patient@qlockcare.dev` | `PatientDevPass123!` |

The script writes the IDs + credentials to `.env.test`.

### 3. Start the API

```bash
uv run uvicorn src.main:app --reload --port 8001
```

### 4. Import the collection + environment

In Postman:

1. **File → Import → Upload Files** → drag in
   `postman/QlockCare_API.postman_collection.json` and
   `postman/environments/Local.postman_environment.json`.
2. Top-right environment dropdown → pick **Local**.

### 5. Log in

Open the `auth` folder → click **Login** → click **Send**.

Watch the lower-right **Environment** panel — `access_token`,
`refresh_token`, `user_id`, `agency_id`, `user_role` should populate.

All subsequent requests auto-inherit the bearer token via the
collection-level auth helper.

---

## Folder layout (organized by user role)

| Folder | Auth | Role required |
|---|---|---|
| `auth` | none | public — login, refresh, logout, OTP, password reset |
| `health` | none | public — `/health`, `/ready` |
| `staff` | bearer | AGENCY_ADMIN |
| `patients` | bearer | AGENCY_ADMIN |
| `appointments` | bearer | AGENCY_ADMIN (create/transition), PATIENT (confirm, request-reschedule) |
| `visits` | bearer | STAFF (check-in/out), PATIENT (verify, dispute, report-issue) |
| `portal` | bearer | **PATIENT** — these will 403 with AGENCY_ADMIN |
| `notifications` | bearer | any authenticated user (broadcast is AGENCY_ADMIN) |
| `locations` | bearer | AGENCY_ADMIN |
| `audit-logs` | bearer | SUPER_ADMIN |

The `portal/` folder is the one place you'll need to switch roles:

1. Open `auth > Login`.
2. Change the email to `patient@qlockcare.dev` / `PatientDevPass123!`.
3. Send. The env's `user_role` becomes `PATIENT`.
4. Open `portal > List my visits` — it now works.

---

## How auto-extract works

When you send a `POST /staff` (or `POST /patients`, `POST /appointments`,
`POST /visits`), the response's `id` is written to the env's
`staff_id` (or `patient_id`, `appointment_id`, `visit_id`).

Subsequent requests in the same folder use `{{staff_id}}` in their URL,
so they automatically target the just-created resource.

Example flow inside the `staff/` folder:

```
Create staff           → env.staff_id = <new uuid>
Get staff by id        → /staff/{{staff_id}}
Update staff           → PATCH /staff/{{staff_id}}
Archive staff (DELETE) → DELETE /staff/{{staff_id}}
```

No copy-paste needed.

---

## Auto token refresh

The collection has a pre-request script that runs before every request:

1. If `access_token` is missing or expired (`access_token_expires_at < now`), and
2. a `refresh_token` is available, and
3. `base_url` is set,

then it tries `POST /auth/refresh` to get a new pair. You don't need to
re-login after 15 minutes — it just works.

To force a re-login: in Postman, click the environment name →
"Edit" → clear `access_token` and `refresh_token` → save.

---

## Running the collection from the command line (Newman)

```bash
# Install Newman once
npm install -g newman newman-reporter-html

# Run against the Local env, see the full pass (no --bail)
NEWMAN_NO_BAIL=1 bash scripts/run_newman.sh postman/environments/Local.postman_environment.json

# Run against the CI env (used by GitHub Actions)
bash scripts/run_newman.sh postman/environments/CI.postman_environment.json
```

Reports are written to `postman/reports/`:

- `run.json` — full machine-readable result
- `run.html` — human-readable HTML report

Open `run.html` in your browser to drill into failures.

---

## Troubleshooting

### `connect ECONNREFUSED 127.0.0.1:8001`

The API isn't running. Start it with `uv run uvicorn src.main:app --port 8001`.

### `401 Unauthorized` on every request

The env's `access_token` is empty or stale. Either:

1. Open `auth > Login` and click Send.
2. Or check that the env dropdown shows **Local** (not "No environment").

### `404 NOT_FOUND` for a just-created resource

You probably hit `Archive staff (DELETE)` or `Delete patient` first.
Click `Create staff` again to get a fresh `staff_id`.

### `403 FORBIDDEN` in the `portal/` folder

You're logged in as AGENCY_ADMIN. Switch to the seeded PATIENT
credentials.

### `409 DUPLICATE_RESOURCE` on `Create patient — duplicate code`

That request is intentional — it's the negative-path test. Run
`Create patient` (the one above it) instead to get a fresh
`patient_id`.

---

## Regenerating the collection

The collection JSON is generated by `postman/_build_collection.py`. If
you add routes or change the test scripts:

```bash
uv run python postman/_build_collection.py
git add postman/QlockCare_API.postman_collection.json
```

The script is intentionally part of the repo (rather than a one-time
bootstrap) so the collection stays in sync with the route surface —
whenever you touch the routers, regenerate.

---

## Files

| Path | Purpose |
|---|---|
| `QlockCare_API.postman_collection.json` | The collection — what Postman / Newman consume. |
| `environments/Local.postman_environment.json` | Dev (port 8001). |
| `environments/CI.postman_environment.json` | CI (port 8000). |
| `environments/Staging.postman_environment.json` | Staging placeholder — fill in real URL. |
| `_build_collection.py` | Generator. Re-run after route changes. |
| `README.md` | This file. |
| `reports/` | Newman output (gitignored). |
