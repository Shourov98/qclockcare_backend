"""Auth router — POST /auth/* endpoints (ADR-0016).

Endpoints:
  POST /auth/login                  → {access, refresh, ...}
  POST /auth/refresh                → {access, refresh, ...}
  POST /auth/logout                 → 204
  POST /auth/accept-invitation      → {sent: true, expires_in}
  POST /auth/verify-email           → {access, refresh, ...}
  POST /auth/resend-otp             → {sent, cooldown_seconds_remaining}
  POST /auth/forgot-password        → {sent: true}
  POST /auth/reset-password         → 204
  GET  /auth/me                     → {user}

All routes use the public `get_session` dependency (no auth required).
`/auth/me` uses `get_session_with_auth` so it both authenticates and
sets RLS GUCs in one go.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.database import get_session
from src.modules.auth import email_service as auth_email
from src.modules.identity import auth_service
from src.modules.identity.dependencies import (
    CurrentAuth,
    get_session_with_auth,
)
from src.modules.identity.schemas import (
    AcceptInvitationRequest,
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

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent")


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------
@router.post("/login", response_model=TokenPair)
async def login_endpoint(
    payload: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> TokenPair:
    issued = await auth_service.login(
        session,
        email=payload.email,
        password=payload.password,
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
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
@router.post("/refresh", response_model=TokenPair)
async def refresh_endpoint(
    payload: RefreshRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> TokenPair:
    issued = await auth_service.refresh(
        session,
        refresh_token=payload.refresh_token,
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
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
@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout_endpoint(
    payload: LogoutRequest,
    request: Request,
    ctx: CurrentAuth,
    session: AsyncSession = Depends(get_session),
) -> None:
    await auth_service.logout(
        session,
        user_id=ctx.user_id,
        refresh_token=payload.refresh_token,
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
    )


# --------------------------------------------------------------------------
# Accept invitation (step 1 of onboarding)
# --------------------------------------------------------------------------
@router.post(
    "/accept-invitation",
    status_code=status.HTTP_202_ACCEPTED,
)
async def accept_invitation_endpoint(
    payload: AcceptInvitationRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> dict:
    user, otp = await auth_service.accept_invitation(
        session,
        invitation_token=payload.invitation_token,
        new_password=payload.password,
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
    )
    # Schedule the OTP email to be sent after the response is flushed.
    # The SMTP call runs in the background via FastAPI's
    # BackgroundTasks (see src/modules/auth/email_service.py) so an
    # unreachable SMTP server cannot block this endpoint.
    # When `LOG_INCLUDE_DEV_OTPS=true`, the OTP is also logged at
    # INFO so devs can test without configuring SMTP.
    if otp is not None:
        auth_email.send_otp_email(
            background_tasks,
            to_email=user.email,
            to_name=user.full_name,
            otp=otp,
            expires_in_minutes=settings.OTP_EXPIRY_MINUTES,
            recipient_user_id=user.id,
        )
    return {
        "accepted": True,
        "email": user.email,
        "otp_sent": True,
    }


# --------------------------------------------------------------------------
# Verify email (step 2 of onboarding)
# --------------------------------------------------------------------------
@router.post("/verify-email", response_model=VerifyEmailResponse)
async def verify_email_endpoint(
    payload: VerifyEmailRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> VerifyEmailResponse:
    issued = await auth_service.verify_email(
        session,
        email=payload.email,
        otp=payload.otp,
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
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
@router.post("/resend-otp", response_model=ResendOtpResponse)
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
@router.get("/me", response_model=MeResponse)
async def me_endpoint(
    ctx: CurrentAuth,
    session: AsyncSession = Depends(get_session_with_auth),
) -> MeResponse:
    # The dependency has already verified the token, loaded the user, and
    # set RLS GUCs. We just need to return the user.
    user = await auth_service.me(session, user_id=ctx.user_id)
    return MeResponse(user=user)


__all__ = ["router"]
