# QlockCare Backend — Client Guide

> A plain-English guide to what the QlockCare backend does, who can use it,
> and how the major workflows unfold. No technical background required.
>
> Last updated: 2026-07-11
> Companion files for engineers: `docs/flow/` (workflow diagrams) and the
> auto-generated OpenAPI page at `/docs` on a running server.

---

## 1. What QlockCare does, in one paragraph

QlockCare is a software platform for **home-care agencies**. Each agency
sends caregivers ("staff") into people's homes to deliver services like
personal care, mental-health support, and 245D-licensed programs. The
backend is the brain of the platform: it stores who the agency is, who
works for them, who their patients are, when services are scheduled, what
happened during each visit, and every notification that goes out — all
in a way that respects strict privacy rules between agencies (each agency
sees only its own data, never another agency's).

---

## 2. Who can use the system

There are **five kinds of users**. Every user is one of these:

| Role | Plain-English meaning |
|---|---|
| **SUPER_ADMIN** | The platform owner. Runs the whole system; can see and manage every agency. |
| **AGENCY_ADMIN** | The boss of one agency. Invites staff, admits patients, sets the agency's locations and preferences. Sees everything inside their agency. |
| **STAFF** | A caregiver. Sees their own schedule, checks in when they arrive at a visit, records what services were delivered. |
| **PATIENT** | The person receiving care. Sees their own upcoming visits, confirms they happened, and can raise a dispute if something was wrong. |
| **GUARDIAN** | A family member or representative who helps a patient manage their account. Acts on the patient's behalf. |

If you picture the system as a building:
- **SUPER_ADMIN** is the building manager (one person for the whole building).
- **AGENCY_ADMIN** is the office manager of one company renting a floor.
- **STAFF** are the company's employees doing the work.
- **PATIENT** is the client receiving a service.
- **GUARDIAN** is the client's adult child or case worker who helps them.

---

## 3. The main things the system keeps track of

| Concept | Plain-English meaning |
|---|---|
| **Agency** | A care agency — the company that owns the floor of the building. Every other record belongs to exactly one agency. |
| **User** | A login identity (an email and a password). One person may be both a STAFF member and a GUARDIAN. |
| **Staff profile** | The "caregiver at agency X" hat a User wears when they work for one agency. Holds their qualifications, availability windows, and a staff code. |
| **Patient profile** | The "person receiving care from agency X" hat a User wears when they are admitted. Holds demographics, admission/discharge info. |
| **Guardian profile** | The "trusted person who can act for a patient" hat. Always linked to one or more patients. |
| **Location** | A service-delivery address belonging to an agency — a patient's home, a community centre, etc. |
| **Appointment** | A scheduled visit: "On Tuesday at 2pm, caregiver Sara will visit patient John at his home and deliver Personal Care for one hour." |
| **Visit** | The actual record of what happened during the visit — when Sara arrived, what services she delivered, when she left. |
| **Service item** | A single line item under an appointment or visit (e.g. "bathing assistance 30 min"). Tracked as done / skipped / needs follow-up. |
| **Notification** | A message sent to a user — appointment reminder, password reset, status update — via in-app, email, SMS, or push. |
| **Audit log** | A tamper-proof history of every important business action. "Who did what to which record, and when." |

---

## 4. The big workflows, told as a story

The system follows a predictable rhythm for every home-care visit. Here's
the typical journey, in plain English, with the real API path each step
hits under the hood.

### 4.1 Onboarding — first time a user signs in

1. **Agency is created** by the SUPER_ADMIN. The agency gets a name, a
   timezone, and a list of programs it offers (PCA, CFSS, 245D, etc.).
2. **The agency's first admin signs in for the first time** using an
   emailed invitation link. They set their own password and the system
   activates their account.
3. **The admin invites staff** by entering their email. The system
   emails each staff member an invitation link, they set a password,
   and they're in.
4. **The admin admits patients** and links guardians where needed.

Behind the scenes: `/auth/login`, `/auth/accept-invitation`,
`/staff`, `/patients`, `/guardians`, `/patient-guardian-relationships`.

### 4.2 Scheduling — "we need a visit next Tuesday"

1. The AGENCY_ADMIN (or an auto-scheduler) **creates an appointment**
   with the patient, the assigned caregiver, the time, and the services
   to be delivered.
2. The system **notifies the patient** (and any guardian) with the
   upcoming appointment details — sent as in-app + email + SMS by default,
   but each user can opt out of any channel.
3. The patient or guardian **confirms**, or **requests a reschedule** or
   **cancellation**.
4. The assigned caregiver **accepts** the appointment, transitioning it
   to `ASSIGNED`.
5. The system keeps state clean: each transition (DRAFT → SCHEDULED →
   NOTIFICATION_SENT → AWAITING_CONFIRMATION → CONFIRMED → ASSIGNED → …)
   is a deliberate step, not a free-form status. Bad transitions are
   rejected.

Behind the scenes: `/appointments`, `/appointments/{id}/transition`,
`/appointments/{id}/assign`, `/appointments/{id}/confirm`,
`/appointments/{id}/request-reschedule`,
`/appointments/{id}/request-cancellation`,
`/appointments/{id}/service-items`,
`/notifications/broadcast`.

### 4.3 Delivery — "the visit is happening now"

1. The caregiver **arrives at the location** and checks in. The system
   records the check-in time and GPS location (if the location has a
   geo-fence, the system validates they're actually there).
2. The visit moves to **IN PROGRESS**. The caregiver can record notes
   and adjust the service items — adding extras, marking items as
   delivered, or flagging follow-ups.
3. When done, the caregiver **checks out**. The system records the
   check-out time, computes the visit duration, and moves the visit to
   COMPLETED.
4. Throughout, **issues** (equipment missing, patient unwell, etc.) can
   be logged. Issues can be resolved by the admin without blocking the
   visit's completion.

Behind the scenes: `/visits`, `/visits/{id}/check-in`,
`/visits/{id}/check-out`, `/visits/{id}/transition`,
`/visits/{id}/service-items`, `/visits/{id}/notes`,
`/visits/{id}/issues`, `/visits/{id}/issues/{issue_id}/resolve`.

### 4.4 Verification — "did the visit really happen?"

1. After the visit is complete, the system asks the **patient or
   guardian** to verify it: "Yes, Sara came on Tuesday and delivered
   the services." This step is **idempotent** — sending the same answer
   twice is safe.
2. If everything's good → the visit moves to **SERVICE_VERIFIED** and on
   toward billing.
3. If something was wrong, the patient/guardian **disputes** the visit,
   giving a reason code (no-show, wrong services, late, etc.). The
   system moves the visit to **DISPUTED → UNDER_REVIEW**, and an
   AGENCY_ADMIN investigates.
4. Admins can also **file a positive verification** on the patient's
   behalf (e.g. phone call follow-up).

Behind the scenes: `/portal/visits`, `/portal/visits/{id}/verify`,
`/portal/visits/{id}/dispute`, `/portal/visits/{id}/report-issue`,
`/visits/{id}/verify`.

### 4.5 Notifications — "tell the right people the right things"

The system fires notifications automatically when:
- an appointment is created, assigned, confirmed, rescheduled, cancelled;
- a visit is checked in, checked out, verified, disputed;
- a staff member is invited;
- a password reset is requested.

Users can:
- see all their notifications on a paginated list;
- see an **unread badge** count (perfect for a top-bar indicator);
- **mark one or all as read**;
- opt out of any (notification type, channel) pair — for instance, a
  patient can say "send me appointment reminders by email only, not
  SMS."

Behind the scenes: `/notifications`, `/notifications/badge`,
`/notifications/preferences`,
`/notifications/preferences/{type}/{channel}`,
`/notifications/{id}/read`, `/notifications/read-all`,
`/notifications/broadcast` (admin-triggered fan-out).

### 4.6 Audit trail — "who did what?"

Every meaningful business action writes one row to the audit log:
- appointment created / updated / transitioned / cancelled
- staff invited / archived
- patient admitted / archived
- visit checked in / out / verified / disputed
- notification broadcast
- user logged in / out / changed password

The log is **append-only** — once written, a row cannot be edited or
deleted. Admins can read the log filtered by actor, resource type, or
time range; SUPER_ADMIN can see all agencies' logs at once.

Behind the scenes: `/audit-logs`, `/audit-logs/{id}`.

---

## 5. How data stays private between agencies

The system has a strict **tenant-isolation** rule baked into every query:

- AGENCY_ADMIN, STAFF, PATIENT, GUARDIAN only ever see rows that belong
  to **their** agency. They literally cannot query another agency's
  data — the database itself rejects the query.
- SUPER_ADMIN is the only role that can see across agencies.
- The privacy rule covers appointments, visits, staff, patients,
  notifications, locations, audit logs — everything except agency
  records themselves.

This is enforced at the database level using PostgreSQL row-level
security (RLS), so even a buggy line of code can't accidentally leak
data between agencies.

---

## 6. How to talk to the backend

Every API call needs a **bearer token** in the `Authorization` header:

```
Authorization: Bearer <token>
```

You get a token by calling `POST /auth/login` with your email and
password. The token is good for 15 minutes; you can refresh it without
re-entering your password by calling `POST /auth/refresh` with your
refresh token.

Every successful response looks like:

```json
{ "data": { ... }, "pagination": { "page": 1, "page_size": 20, "total": 47, "total_pages": 3 } }
```

Every error looks like:

```json
{
  "error": {
    "code": "NOT_FOUND",
    "message": "Agency not found",
    "request_id": "5f3a7b1c-...",
    "timestamp": "2026-07-11T10:23:01Z"
  }
}
```

The error envelope always includes a `request_id` and a matching
`X-Request-ID` response header — when you report a problem to support,
include this ID so they can find the exact server-side trace.

Common error codes you'll see:

| HTTP | Code | What it means |
|---|---|---|
| 401 | `UNAUTHORIZED` | Token missing, expired, or malformed. |
| 403 | `INSUFFICIENT_PERMISSIONS` | Your role can't do this action. |
| 403 | `CROSS_AGENCY_ACCESS_DENIED` | The record belongs to another agency. |
| 404 | `NOT_FOUND` | The record doesn't exist, or it was archived. |
| 409 | `INVALID_STATE_TRANSITION` | You tried to skip a step (e.g. mark a visit complete before check-out). |
| 422 | `VALIDATION_ERROR` | Your request body or query string is invalid. |
| 429 | `RATE_LIMIT_EXCEEDED` | You sent too many requests in a short window — slow down. |

---

## 7. The end-to-end journey, in one picture

```
                         ┌─────────────────────┐
                         │  SUPER_ADMIN        │
                         │  creates an Agency  │
                         └──────────┬──────────┘
                                    │
              ┌─────────────────────┼─────────────────────┐
              │                     │                     │
        invite staff         admit patients        set locations
              │                     │                     │
              ▼                     ▼                     ▼
       ┌──────────┐         ┌──────────────┐       ┌──────────┐
       │  STAFF   │         │   PATIENT    │       │ Location │
       └─────┬────┘         └──────┬───────┘       └────┬─────┘
             │                     │                    │
             └──────── schedule ───┼──── at ────────────┘
                                   ▼
                          ┌────────────────┐
                          │  APPOINTMENT   │
                          └───────┬────────┘
                                  │ notify
                                  ▼
                         patient confirms
                                  │
                                  ▼
                            caregiver arrives
                                  │
                                  ▼
                       ┌────────────────────┐
                       │       VISIT        │
                       │  check-in → work → │
                       │       check-out    │
                       └─────────┬──────────┘
                                 │
                          patient verifies
                          (or disputes)
                                 │
                                 ▼
                          → billing pipeline
```

---

## 8. What you can find where

| You want to know… | Open this file |
|---|---|
| Every API endpoint, with request and response schemas | http://127.0.0.1:8001/docs (live server) |
| Workflow diagrams for any module | `docs/flow/*_flow.svg` |
| What each API does in one paragraph | the sections above |
| The technical contract (status codes, payloads, RLS) | OpenAPI JSON at `/openapi.json` |
| Local development setup | `docs/flow/`'s parent `README.md` and `/scripts/` |

---

## 9. What's NOT in this guide (yet)

The team has **planned but not shipped** the following admin surfaces —
they're being built in the feature branches and will land here once
merged:

- A SUPER_ADMIN-only **agency management** surface (create, list, edit,
  archive agencies, and the programs each agency offers). Currently
  agencies are created via direct database seeding.
- A **POST /auth/change-password** endpoint for logged-in users.
- A `GET /portal/patients/me` self-service profile endpoint.
- Self-service guardian linking from the patient portal.
- A bulk **POST /audit-logs/export** endpoint (CSV download of audit
  rows matching a filter).
- A bundled **Dockerfile** and **docker-compose.yml** for one-line
  local stack bring-up.
- A **contributing guide** and a **deployment guide**.

If you need any of these and can't wait, ask the team — they're tracked
in the implementation checklist and several are mid-flight on
feature branches.

---

## 10. Glossary

- **Bearer token** — a long random string you send on every request to
  prove who you are.
- **Idempotent** — you can do it twice and get the same result; the
  system won't charge you twice or create duplicates.
- **RLS (Row-Level Security)** — a database feature that hides rows from
  you unless you're allowed to see them, enforced at the DB layer so
  application bugs can't bypass it.
- **Soft-delete / archive** — marking a record as deleted instead of
  erasing it, so historical references (appointments, audit logs) still
  point to it.
- **Soft-delete with `deleted_at`** — the technical name for the
  archive pattern: a record with a non-null `deleted_at` column is
  treated as gone, but its row stays in the table.

---

*If anything in this guide is unclear, out of date, or missing, please
flag it to the engineering team — this is a living document.*