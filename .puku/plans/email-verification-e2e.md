# Email Verification E2E — Plan

## Context

OTP infrastructure is fully in place (`src/modules/identity/otp_service.py`):
issue / hash / verify / resend / cooldown / max-attempts / audit. The
`/auth/verify-email` endpoint already transitions the user to ACTIVE and
sets `email_verified_at`.

**But the OTP is never actually emailed.** Today:

- `POST /auth/accept-invitation` (`src/modules/identity/router.py:130`)
  returns the OTP in the response body under the key `dev_otp` with a
  `DEV ONLY: remove once SMTP is wired` comment.
- `POST /auth/resend-otp` issues a fresh OTP and discards it.
- `POST /auth/forgot-password` issues a `SingleUseToken` (purpose
  `password_reset`) and only logs it.

Goal: **end-to-end email verification.** When a user accepts an
invitation, resends their OTP, or requests a password reset, the OTP /
reset link must be emailed to them — not returned in the API response.

We have a working outbound SMTP path: `EmailProvider.send`
(`src/modules/notifications/channels.py:103`) calls `aiosmtplib.send`
with `timeout=SMTP_TIMEOUT_SECONDS`. The just-shipped
`feat/background-notification-dispatch` PR made the SMTP call safe to
use from any code path (request thread can no longer hang on an
unreachable server).

## Scope

1. Send the OTP / reset email from `accept-invitation`,
   `resend-otp`, `forgot-password`. Skip the `notifications` table
   (no bell-icon entry for transactional auth emails).
2. Plain-text email body — no template engine, no HTML. Matches the
   current `EmailProvider` shape (`EmailMessage.set_content(body)`).
3. Remove `dev_otp` from the `accept-invitation` response.
4. Add `FRONTEND_URL` setting so the email contains a clickable deep
   link to the SPA verify page (`/verify-email?email=...&otp=...`).
5. Enforce a 60-second cooldown on `/auth/forgot-password` (mirrors
   `OTP_RESEND_COOLDOWN_SECONDS`).
6. End-to-end integration test that exercises accept-invitation → OTP
   arrives via a sentinel SMTP provider.

Out of scope: HTML templates, unsubscribe links, bounce handling,
retry-with-backoff for failed SMTP sends (covered by the
just-shipped Phase 2 follow-up).

## Design

### Where the code lives

New module: `src/modules/auth/email_service.py`. (Yes — `auth` is a
new top-level module. We currently have no `src/modules/auth/`. But
`src/modules/identity/` is reserved for auth-mechanics on the User
model. Auth-related *transactional emails* deserve their own module
because they don't fit cleanly inside `identity/` and they're the
only consumer of the deep-link email builder.)

Functions:

- `send_otp_email(background_tasks, *, to_email, to_name, otp,
  expires_in_minutes: int) -> None`
  Builds the EmailMessage (subject `Verify your QlockCare account`,
  body includes the OTP + a clickable `${FRONTEND_URL}/verify-email?
  email=...&otp=...` link + the expiry). Schedules the network call
  via `BackgroundTasks.add_task(_send_in_background, ...)`.

- `send_password_reset_email(background_tasks, *, to_email, to_name,
  reset_token, expires_in_minutes: int) -> None`
  Same shape. Subject `Reset your QlockCare password`, body links to
  `${FRONTEND_URL}/reset-password?token=...`.

Both functions:

- Build the `EmailMessage` synchronously (no I/O).
- Call `background_tasks.add_task(_send_in_background, ...)` —
  `_send_in_background` opens a fresh session via `session_scope()`,
  re-establishes minimal RLS context (recipient_user_id + role =
  `"SYSTEM"`, agency_id=None), then calls
  `EmailProvider().send(...)` directly. No `Notification` row is
  created.
- Catch every exception inside `_send_in_background` and log — never
  raises back to the caller. (The request thread is decoupled; the
  user is told "email sent" optimistically.)

### Why a separate background runner

The just-shipped `notifications/background.py:run_dispatch_in_background`
loads a `Notification` row, calls the providers, UPDATEs
`notification_deliveries`. None of that applies here — we have no
notification row, and we don't want one. A dedicated
`auth.email_service._send_in_background` is the minimal correct shape.

### RLS context for the background session

The auth router uses `Depends(get_session)` — no JWT, no GUCs. The
background runner opens a fresh session and must satisfy RLS for any
DB writes it does. **We do not write any rows** in the auth email
path (no Notification, no audit row in the email step itself — the
audit happens in the request thread via `_record_audit`, which today
already runs without GUCs; that's a pre-existing quirk we leave alone
for this PR). The background session is read-only on `users` (to
look up the recipient's name if needed) — and `users` SELECT has no
RLS restriction, only writes. So:

- The background session sets `current_user_id = <recipient_user_id>`,
  `current_user_role = 'SYSTEM'`, `current_agency_id = NULL`. This
  is sufficient to satisfy any future RLS we add on the read path
  for `users`.

For now: `set_session_context(session, user_id=str(recipient_user_id),
agency_id=None, user_role='SYSTEM')` exactly mirrors what
`set_session_context` already does
(`src/core/database.py:96-126`). We do NOT add a new migration or
service-role bypass in this PR.

### Cooldown for forgot-password

Today, `auth_service.forgot_password`
(`src/modules/identity/auth_service.py:590-627`) issues a fresh
`SingleUseToken` on every call with no throttle. Add a check: read the
user's last `PASSWORD_RESET_REQUESTED` audit event
(`auth_audit_events`), and if its timestamp is within
`OTP_RESEND_COOLDOWN_SECONDS`, raise `OtpResendCooldownError` (reuse
the existing exception — it's already mapped to HTTP 429 in the
exception handler). This piggybacks on the audit log that
`_record_audit` already writes, so no schema change.

### Cooldown for resend-otp

Already enforced inside `otp_service.resend_otp` (line 222-230). No
change.

### Response body cleanup

- `accept_invitation_endpoint`: drop `dev_otp` from the response.
  Response becomes `{accepted: true, email: user.email, otp_sent:
  true}`. Update `AcceptInvitationResponse` in
  `src/modules/identity/schemas.py` to drop `dev_otp` (or remove the
  `dev_otp` field; keep `accepted` + `email` + `expires_in`).
- `forgot_password_endpoint`: drop `dev_token`. Body becomes
  `{sent: true}`.

### When SMTP is disabled

If `SMTP_ENABLED=false` (the default in dev), `EmailProvider.send`
returns a `DeliveryResult(success=False, error="SMTP disabled...")`
inside the background task. The HTTP response is already returned by
then (we returned 202), so the caller sees "sent=true" but the email
never lands. This is the same optimistic-ack pattern every
transactional-email system uses. For dev we keep `dev_otp` available
through the OTP-Sent audit log so developers can still complete the
flow without configuring SMTP — see "Dev escape hatch" below.

### Dev escape hatch

To keep local dev usable without configuring SMTP, log the OTP /
reset token in the application log at INFO level with the structured
field `dev_otp_for_test_only=...` inside the background runner. The
log message includes a clear "DEV ONLY — never appears in production"
prefix. This replaces the current `dev_otp` field in the response.

Production can suppress by setting a new
`LOG_INCLUDE_DEV_OTPS: bool = False` setting (default False, so it's
safe out of the box).

## Files

### New
- `src/modules/auth/__init__.py`
- `src/modules/auth/email_service.py` — `send_otp_email`,
  `send_password_reset_email`, `_send_in_background`,
  `_build_otp_email`, `_build_reset_email`, `LOG_INCLUDE_DEV_OTPS`.

### Modified
- `src/core/config.py` — add `FRONTEND_URL: str` (default
  `"http://localhost:3000"`) + `LOG_INCLUDE_DEV_OTPS: bool = False`.
- `.env.example` — add `FRONTEND_URL=` line.
- `src/modules/identity/router.py` —
  - `accept_invitation_endpoint`: take `BackgroundTasks`, call
    `auth.email_service.send_otp_email`, drop `dev_otp` from
    response.
  - `resend_otp_endpoint`: take `BackgroundTasks`, fetch the new
    OTP (extend `auth_service.resend_otp` to also return the
    plaintext OTP), call `send_otp_email`.
  - `forgot_password_endpoint`: take `BackgroundTasks`, call
    `send_password_reset_email`, enforce cooldown (raise
    `OtpResendCooldownError`).
- `src/modules/identity/auth_service.py` —
  - Extend `forgot_password(...)` to enforce
    `OTP_RESEND_COOLDOWN_SECONDS` cooldown via the
    `auth_audit_events` table.
  - Extend `resend_otp(...)` return type to include the new
    OTP plaintext (`OtpIssueResult`) — easiest done by changing
    the return type to `(int, OtpIssueResult | None)` so the
    cooldown path can still return 0 without an OTP.
- `src/modules/identity/schemas.py` — drop `dev_otp` from
  `AcceptInvitationResponse`, drop `dev_token` from
  `ForgotPasswordResponse`. Add `cooldown_seconds_remaining` to
  `ForgotPasswordResponse` so clients can show a "try again in N
  seconds" message on 429.

### Tests
- `tests/unit/test_auth_email_service.py` — new. Verify the
  `EmailMessage` is built correctly (subject, From, To, body
  contains the OTP + the deep-link URL). Mock `EmailProvider` to
  capture the call. Verify `LOG_INCLUDE_DEV_OTPS=true` causes the
  OTP to appear in the log.
- `tests/integration/test_auth_flow.py` — extend. Add
  `test_accept_invitation_emails_otp`: register a user with the
  sentinel `_CapturingEmailProvider` registered in the
  `ProviderRegistry`, hit `POST /auth/accept-invitation`, assert
  the provider was called with the right OTP + email. Mirror the
  `_HangingProvider` pattern from
  `tests/integration/test_notifications_deliveries_flow.py:290`.
- `tests/integration/test_auth_flow.py` — extend. Add
  `test_forgot_password_rate_limited`: two `POST
  /auth/forgot-password` calls within the cooldown → second
  returns 429 with `cooldown_seconds_remaining > 0`.

## Reused utilities

- `aiosmtplib` via `EmailProvider.send`
  (`src/modules/notifications/channels.py:103-148`) — wire-format
  and SMTP timeout are already correct.
- `BackgroundTasks.add_task(...)` pattern from
  `src/modules/notifications/integrations.py:43-65` — copy the
  shape into `email_service.py`. (We don't import from
  `notifications.background` because that helper is hard-coded to
  load a `Notification` row.)
- `set_session_context`
  (`src/core/database.py:96-126`) — used by the background runner
  to satisfy RLS on any future reads.
- `OtpResendCooldownError` + `OtpIssueResult` from
  `src/modules/identity/otp_service.py`.
- `OtpResendCooldownError` is already mapped to HTTP 429 by the
  global exception handler — no router changes needed for the
  cooldown.

## Verification

### Pre-merge (run locally)

1. `alembic upgrade head` — no schema change; should be no-op.
2. `uv run pytest tests/unit/ -q` — full unit suite green.
3. `uv run pytest tests/integration/test_auth_flow.py -q` — new
   accept-invitation-email + forgot-password-cooldown tests
   green. Skipped if no local Supabase.
4. `uv run ruff check src/modules/auth/ src/modules/identity/
   src/core/config.py tests/unit/test_auth_email_service.py` —
   clean.

### Manual smoke (against the dev app)

1. `SMTP_ENABLED=true SMTP_HOST=127.0.0.1 SMTP_PORT=1025 uv run
   uvicorn src.main:app` — start a fake SMTP server on :1025 (or
   use `python -m aiosmtplib -n`); hit
   `POST /auth/accept-invitation` and confirm the request returns
   in <1s (not 30s) and the fake SMTP server logs the OTP email.
2. `LOG_INCLUDE_DEV_OTPS=true` — confirm the OTP appears in the
   app log under the structured key `dev_otp_for_test_only` so
   dev can still test without configuring SMTP.
3. Hit `/auth/forgot-password` twice within 60s — confirm the
   second returns 429 with `cooldown_seconds_remaining`.

### Post-merge smoke

1. `curl POST /auth/accept-invitation` — returns 202 within 100ms
   with no `dev_otp` in the body.
2. The user's inbox receives the OTP email with the deep link to
   `${FRONTEND_URL}/verify-email?...`.
3. `POST /auth/verify-email {email, otp}` — returns 200 with the
   token pair, transitioning the user to ACTIVE.

## Branch

`feat/email-verification-e2e` cut from `develop`
(post-#14 background-dispatch merge).