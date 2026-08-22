"""Auth router — POST /auth/* endpoints (ADR-0016).

Endpoints:
  POST /auth/login                  → {access, refresh, ...}
  POST /auth/refresh                → {access, refresh, ...}
  POST /auth/logout                 → 204
  POST /auth/accept-invitation      → {access, refresh, ...}
  POST /auth/verify-email           → {access, refresh, ...}
  POST /auth/resend-otp             → {sent, cooldown_seconds_remaining}
  POST /auth/forgot-password        → {sent: true}
  POST /auth/reset-password         → 204
  POST /auth/change-password        → 204         (authenticated)
  GET  /auth/me                     → {user}

All routes use the public `get_session` dependency (no auth required).
`/auth/me` uses `get_session_with_auth` so it both authenticates and
sets RLS GUCs in one go.

Authentication is dual-mode:
  * Bearer (`Authorization: Bearer <jwt>`) for non-browser clients
  * HttpOnly cookies (`qc_access`, `qc_refresh`) + CSRF token
    (`X-CSRF-Token` header vs. `qc_csrf` cookie) for browser SPAs

Bearer and cookie credentials are interchangeable for read endpoints;
cookie-authenticated write requests must echo the CSRF token.

Every route attaches `summary=` (short), `description=` (long-form
markdown), and `responses=` (pre-wired 401/403/422 examples via
`standard_responses(...)`) so `/docs` shows the operation in the
sidebar with realistic payloads.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.database import get_session
from src.core.exceptions import UnauthorizedError
from src.modules.identity import auth_service
from src.modules.identity.cookies import (
    QC_REFRESH_COOKIE,
    clear_auth_cookies,
    csrf_protect,
    set_auth_cookies,
)
from src.modules.identity.dependencies import (
    CurrentAuth,
    get_session_with_auth,
)
from src.modules.identity.schemas import (
    AcceptInvitationRequest,
    ChangePasswordRequest,
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    LoginRequest,
    LogoutRequest,
    MeResponse,
    RefreshRequest,
    ResendOtpRequest,
    ResendOtpResponse,
    ResetPasswordRequest,
    TokenPair,
    VerifyEmailRequest,
    VerifyEmailResponse,
)
from src.shared.schemas.docs import standard_responses

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent")


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------
@router.post(
    "/login",
    response_model=TokenPair,
    responses=standard_responses(include=[401, 403, 422]),
    summary="Log in with email and password",
    description=(
        "Authenticates a user with email + password and returns an "
        "access/refresh token pair. The access token is short-lived "
        "(default 15 minutes); the refresh token is long-lived "
        "(default 30 days). 5 consecutive failures lock the account "
        "for `settings.ACCOUNT_LOCKOUT_MINUTES` minutes. Sets the "
        "`qc_access` / `qc_refresh` / `qc_csrf` cookies."
    ),
)
async def login_endpoint(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> TokenPair:
    issued = await auth_service.login(
        session,
        email=payload.email,
        password=payload.password,
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
    )
    set_auth_cookies(
        response,
        access_token=issued.access_token,
        refresh_token=issued.refresh_token,
        expires_in=issued.expires_in,
    )
    return TokenPair(
        access_token=issued.access_token,
        refresh_token=issued.refresh_token,
        expires_in=issued.expires_in,
        user=issued.user,
    )


# --------------------------------------------------------------------------
# Refresh
# --------------------------------------------------------------------------
@router.post(
    "/refresh",
    response_model=TokenPair,
    responses=standard_responses(include=[401, 403, 422]),
    summary="Mint a fresh access token",
    description=(
        "Exchanges a valid refresh token for a new access/refresh pair. "
        "The refresh token is **rotated** — store the new value and "
        "discard the old one. Old refresh tokens cannot be reused. "
        "Accepts the refresh token in the request body OR via the "
        "`qc_refresh` HttpOnly cookie."
    ),
)
async def refresh_endpoint(
    payload: RefreshRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> TokenPair:
    # Body wins, fall back to cookie. If neither is present the
    # client is unauthenticated — surface a typed 401.
    refresh_token = payload.refresh_token or request.cookies.get(QC_REFRESH_COOKIE)
    if not refresh_token:
        raise UnauthorizedError(
            message="Refresh token required (body or cookie).",
            details={"reason": "missing_refresh_token"},
        )
    issued = await auth_service.refresh(
        session,
        refresh_token=refresh_token,
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
    )
    set_auth_cookies(
        response,
        access_token=issued.access_token,
        refresh_token=issued.refresh_token,
        expires_in=issued.expires_in,
    )
    return TokenPair(
        access_token=issued.access_token,
        refresh_token=issued.refresh_token,
        expires_in=issued.expires_in,
        user=issued.user,
    )


# --------------------------------------------------------------------------
# Logout
# --------------------------------------------------------------------------
@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=standard_responses(include=[401, 403, 422]),
    # csrf_protect is added as a `dependencies=` entry rather than a
    # signature param so it runs but we don't need its return value.
    # Bearer clients bypass via the check inside `csrf_protect`.
    dependencies=[Depends(csrf_protect)],
    summary="Log out (revoke refresh token)",
    description=(
        "Revokes the supplied refresh token (or all active refresh "
        "tokens if `refresh_token` is omitted — useful for "
        "\"log out everywhere\"). The access token in the "
        "`Authorization` header is unaffected and remains valid until "
        "its own expiry. Clears the `qc_access` / `qc_refresh` / "
        "`qc_csrf` cookies regardless of the outcome."
    ),
)
async def logout_endpoint(
    request: Request,
    response: Response,
    ctx: CurrentAuth,
    session: AsyncSession = Depends(get_session),
    payload: LogoutRequest | None = Body(default=None),
) -> None:
    # Resolve refresh token: body wins, fall back to cookie. Authenticated
    # via the bearer/cookie credential in `ctx`, so no 401 here — missing
    # refresh token just means "logout-everywhere" semantics. The body is
    # optional so cookie-authenticated SPAs (which carry the refresh token
    # via the `qc_refresh` cookie) can POST without a JSON payload.
    refresh_token = (
        (payload.refresh_token if payload else None)
        or request.cookies.get(QC_REFRESH_COOKIE)
    )
    await auth_service.logout(
        session,
        user_id=ctx.user_id,
        refresh_token=refresh_token,
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
    )
    clear_auth_cookies(response)


# --------------------------------------------------------------------------
# Accept invitation (single-step onboarding)
# --------------------------------------------------------------------------
@router.post(
    "/accept-invitation",
    response_model=TokenPair,
    responses=standard_responses(include=[401, 404, 409, 422]),
    summary="Accept an invitation, verify OTP, and receive a session",
    description=(
        "Single-step onboarding. The invitee submits the email from "
        "their invitation deep link, the 6-digit code from the email "
        "body, and a new password that satisfies the project password "
        "policy. On success the user is marked `ACTIVE`, the email "
        "is verified, and the response carries a fresh access/refresh "
        "token pair — the client is logged in immediately. No "
        "follow-up `/auth/verify-email` call is required."
    ),
)
async def accept_invitation_endpoint(
    payload: AcceptInvitationRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> TokenPair:
    issued = await auth_service.accept_invitation(
        session,
        email=payload.email,
        otp=payload.otp,
        new_password=payload.password,
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
    )
    set_auth_cookies(
        response,
        access_token=issued.access_token,
        refresh_token=issued.refresh_token,
        expires_in=issued.expires_in,
    )
    return TokenPair(
        access_token=issued.access_token,
        refresh_token=issued.refresh_token,
        expires_in=issued.expires_in,
        user=issued.user,
    )


# --------------------------------------------------------------------------
# Verify email (step 2 of onboarding)
# --------------------------------------------------------------------------
@router.post(
    "/verify-email",
    response_model=VerifyEmailResponse,
    responses=standard_responses(include=[401, 422]),
    summary="Verify the OTP and receive a session",
    description=(
        "Step 2 of onboarding. Submits the 6-digit code from the "
        "welcome email. On success returns a fresh access/refresh "
        "token pair and marks the user's email as verified. "
        "Account is locked after 5 failed attempts. Sets the "
        "`qc_access` / `qc_refresh` / `qc_csrf` cookies."
    ),
)
async def verify_email_endpoint(
    payload: VerifyEmailRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> VerifyEmailResponse:
    issued = await auth_service.verify_email(
        session,
        email=payload.email,
        otp=payload.otp,
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
    )
    set_auth_cookies(
        response,
        access_token=issued.access_token,
        refresh_token=issued.refresh_token,
        expires_in=issued.expires_in,
    )
    return VerifyEmailResponse(
        access_token=issued.access_token,
        refresh_token=issued.refresh_token,
        expires_in=issued.expires_in,
        user=issued.user,
    )


# --------------------------------------------------------------------------
# Resend OTP
# --------------------------------------------------------------------------
@router.post(
    "/resend-otp",
    response_model=ResendOtpResponse,
    responses=standard_responses(include=[422]),
    summary="Resend the verification OTP",
    description=(
        "Issues a fresh OTP to the given email if (a) the account "
        "exists, (b) it isn't already verified, and (c) the cooldown "
        "has elapsed. Returns the same `sent=true` shape either way "
        "to avoid leaking account presence. `cooldown_seconds_remaining` "
        "tells the client how long to wait before the next request."
    ),
)
async def resend_otp_endpoint(
    payload: ResendOtpRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> ResendOtpResponse:
    cooldown, issued = await auth_service.resend_otp(
        session,
        email=payload.email,
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
    )
    # Schedule the new OTP email. We schedule even when `issued` is
    # None (user not found) — the email_service is a no-op in that
    # case since we don't have an OTP to embed. Doing it
    # unconditionally keeps the "don't leak existence" property.
    if issued is not None:
        auth_email.send_otp_email(
            background_tasks,
            to_email=issued.email,
            to_name=issued.full_name,
            otp=issued.otp,
            expires_in_minutes=settings.OTP_EXPIRY_MINUTES,
            recipient_user_id=issued.user_id,
        )
    return ResendOtpResponse(sent=True, cooldown_seconds_remaining=cooldown)


# --------------------------------------------------------------------------
# Forgot password
# --------------------------------------------------------------------------
@router.post(
    "/forgot-password",
    response_model=ForgotPasswordResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=standard_responses(include=[422]),
    summary="Request a password-reset link",
    description=(
        "Sends a password-reset link to the given email if the "
        "account exists. Returns `sent=true` either way to avoid "
        "leaking account presence. Reset tokens expire after 2 hours."
    ),
)
async def forgot_password_endpoint(
    payload: ForgotPasswordRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> ForgotPasswordResponse:
    user_id, email, token = await auth_service.forgot_password(
        session,
        email=payload.email,
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
    )
    # Schedule the reset-link email. Same SMTP-via-BackgroundTasks
    # pattern as the OTP email — see src/modules/auth/email_service.py.
    # `user_id` is None when the email is not registered; we no-op
    # in that case to avoid leaking account existence.
    if user_id is not None and token is not None:
        assert email is not None  # invariant: user_id implies email
        auth_email.send_password_reset_email(
            background_tasks,
            to_email=email,
            to_name=None,  # full_name not loaded by forgot_password path
            reset_token=token,
            # 2-hour TTL matches jwt_service.issue_single_use_token
            # `ttl=timedelta(hours=2)` above.
            expires_in_minutes=120,
            recipient_user_id=user_id,
        )
    return ForgotPasswordResponse(sent=True)


# --------------------------------------------------------------------------
# Reset password
# --------------------------------------------------------------------------
@router.post(
    "/reset-password",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=standard_responses(include=[401, 422]),
    summary="Set a new password with a reset token",
    description=(
        "Consumes a reset token (from the password-reset email) and "
        "sets a new password that satisfies the project password "
        "policy. The token is single-use; subsequent attempts return "
        "`INVALID_RESET_TOKEN`."
    ),
)
async def reset_password_endpoint(
    payload: ResetPasswordRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> None:
    await auth_service.reset_password(
        session,
        reset_token=payload.reset_token,
        new_password=payload.password,
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
    )


# --------------------------------------------------------------------------
# Me
# --------------------------------------------------------------------------
@router.get(
    "/me",
    response_model=MeResponse,
    responses=standard_responses(include=[401, 403]),
    summary="Get the currently authenticated user",
    description=(
        "Returns the `CurrentUser` derived from the bearer token "
        "OR the `qc_access` HttpOnly cookie. Use this on app load "
        "to bootstrap the SPA's user state."
    ),
)
async def me_endpoint(
    ctx: CurrentAuth,
    session: AsyncSession = Depends(get_session_with_auth),
) -> MeResponse:
    # The dependency has already verified the token, loaded the user, and
    # set RLS GUCs. We just need to return the user.
    user = await auth_service.me(session, user_id=ctx.user_id)
    return MeResponse(user=user)


# --------------------------------------------------------------------------
# Change password (authenticated)
# --------------------------------------------------------------------------
@router.post(
    "/change-password",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=standard_responses(include=[401, 422]),
    summary="Change the caller's password while signed in",
    description=(
        "Authenticated callers (any role) rotate their own password "
        "by supplying the *current* password plus a new one that "
        "satisfies the project password policy. On success the "
        "server revokes ALL outstanding refresh tokens for the user, "
        "forcing re-login on every other device. The current browser "
        "continues to work until its access token expires, then "
        "`/auth/refresh` returns 401 and the user is bounced to "
        "`/sign-in`.\n\n"
        "Failed attempts count toward the account lockout threshold "
        "(`settings.ACCOUNT_LOCKOUT_THRESHOLD`) — same behaviour "
        "as `/auth/login`. For password resets by users who *can't* "
        "log in, use `/auth/forgot-password` + `/auth/reset-password` "
        "instead."
    ),
)
async def change_password_endpoint(
    payload: ChangePasswordRequest,
    request: Request,
    ctx: CurrentAuth,
    session: AsyncSession = Depends(get_session_with_auth),
) -> Response:
    await auth_service.change_password(
        session,
        user_id=ctx.user_id,
        current_password=payload.current_password,
        new_password=payload.new_password,
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
    )
    # No body. Refresh-token revocation is a side effect; the client
    # will discover it on its next refresh attempt.
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
