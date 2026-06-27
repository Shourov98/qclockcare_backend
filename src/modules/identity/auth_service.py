"""Auth service — the orchestrator that ties JWT, OTP, password, and user
lookups into the flows defined by ADR-0016.

Each public function corresponds to one HTTP endpoint:

  - login                  → POST /auth/login
  - refresh                → POST /auth/refresh
  - logout                 → POST /auth/logout
  - accept_invitation      → POST /auth/accept-invitation
  - verify_email           → POST /auth/verify-email
  - resend_otp             → POST /auth/resend-otp
  - forgot_password        → POST /auth/forgot-password
  - reset_password         → POST /auth/reset-password

All functions take an `AsyncSession` so the caller controls the transaction
boundary (the FastAPI dependency `get_session` handles commit/rollback).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.exceptions import (
    AccountDisabledError,
    AccountLockedError,
    EmailNotVerifiedError,
    InvalidCredentialsError,
    InvalidInvitationTokenError,
    InvalidResetTokenError,
    InvitationAlreadyConsumedError,
    NotFoundError,
    OtpResendCooldownError,
    UnauthorizedError,
)
from src.core.logging import get_logger
from src.core.security import hash_password, needs_rehash, verify_password
from src.modules.identity import jwt_service, otp_service
from src.modules.identity.models import (
    AuthAuditEvent,
    RefreshToken,
    SingleUseToken,
    User,
    UserRoleAssignment,
)
from src.modules.identity.schemas import CurrentUser
from src.shared.domain.enums import (
    AuthAuditEventType,
    UserRole,
    UserStatus,
)

logger = get_logger(__name__)


# --------------------------------------------------------------------------
# DTOs
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    expires_in: int
    user: CurrentUser


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _role_priority(role: UserRole) -> int:
    """Lower number = more privileged. Used to pick a default role for the
    access token when the user has more than one."""
    return {
        UserRole.SUPER_ADMIN: 0,
        UserRole.AGENCY_ADMIN: 1,
        UserRole.STAFF: 2,
        UserRole.PATIENT: 3,
        UserRole.GUARDIAN: 4,
    }.get(role, 99)


def _pick_primary_role(roles: list[UserRoleAssignment]) -> tuple[UserRole, uuid.UUID | None]:
    """Pick the most-privileged role for the access token.

    SUPER_ADMIN wins. Otherwise, the lowest-priority role wins.
    Returns (role, agency_id).
    """
    if not roles:
        return UserRole.STAFF, None  # shouldn't happen for ACTIVE users
    sa = [r for r in roles if r.role == UserRole.SUPER_ADMIN]
    if sa:
        return UserRole.SUPER_ADMIN, None
    ranked = sorted(roles, key=lambda r: _role_priority(r.role))
    top = ranked[0]
    return top.role, top.agency_id


async def _load_user_with_roles(
    session: AsyncSession, user_id: uuid.UUID
) -> User:
    """Fetch the user with their roles eagerly loaded."""
    from sqlalchemy.orm import selectinload

    stmt = select(User).where(User.id == user_id).options(selectinload(User.roles))
    user = (await session.execute(stmt)).scalar_one_or_none()
    if user is None:
        raise NotFoundError(details={"resource": "user"})
    return user


async def _record_audit(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    event_type: AuthAuditEventType,
    ip_address: str | None = None,
    user_agent: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Append a row to `auth_audit_events`."""
    session.add(
        AuthAuditEvent(
            user_id=user_id,
            event_type=event_type,
            ip_address=ip_address,
            user_agent=user_agent,
            event_metadata=metadata or {},
        )
    )


def _to_current_user(user: User) -> CurrentUser:
    role, agency_id = _pick_primary_role(user.roles)
    return CurrentUser(
        id=str(user.id),
        email=user.email,
        full_name=user.full_name,
        status=user.status.value,
        email_verified=user.email_verified_at is not None,
        agency_id=str(agency_id) if agency_id else None,
        role=role.value,
    )


async def _issue_pair(
    session: AsyncSession,
    *,
    user: User,
    ip_address: str | None,
    user_agent: str | None,
) -> IssuedTokens:
    """Issue access + refresh tokens, persist the refresh row, return pair."""
    role, agency_id = _pick_primary_role(user.roles)
    access_token, expires_in = jwt_service.issue_access_token(
        user_id=user.id,
        email=user.email,
        role=role.value,
        agency_id=agency_id,
    )
    refresh_token, jti, expires_at = jwt_service.issue_refresh_token(user_id=user.id)
    session.add(
        RefreshToken(
            jti=jti,
            user_id=user.id,
            expires_at=expires_at,
            user_agent=user_agent,
            ip_address=ip_address,
        )
    )
    return IssuedTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        user=_to_current_user(user),
    )


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------
async def login(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> IssuedTokens:
    """Verify credentials, enforce lockout, return token pair."""
    from sqlalchemy.orm import selectinload

    user = (
        await session.execute(
            select(User)
            .where(User.email == email)
            .options(selectinload(User.roles))
        )
    ).scalar_one_or_none()

    # Generic error message — don't leak whether the email exists.
    # If the user doesn't exist, still run a hash to make timing similar.
    if user is None:
        hash_password(password)
        await _record_audit(
            session,
            user_id=None,
            event_type=AuthAuditEventType.LOGIN_FAILED,
            ip_address=ip_address,
            user_agent=user_agent,
            metadata={"email": email, "reason": "unknown_email"},
        )
        raise InvalidCredentialsError()

    # Lockout check
    now = datetime.now(tz=UTC)
    if user.locked_until is not None and user.locked_until > now:
        raise AccountLockedError(
            details={"locked_until": user.locked_until.isoformat()}
        )

    # No password yet (still in INVITED state)
    if user.password_hash is None:
        await _record_audit(
            session,
            user_id=user.id,
            event_type=AuthAuditEventType.LOGIN_FAILED,
            ip_address=ip_address,
            user_agent=user_agent,
            metadata={"reason": "password_not_set"},
        )
        raise InvalidCredentialsError()

    # Verify password
    if not verify_password(password, user.password_hash):
        user.failed_login_attempts += 1
        if user.failed_login_attempts >= settings.ACCOUNT_LOCKOUT_THRESHOLD:
            user.locked_until = now + timedelta(
                minutes=settings.ACCOUNT_LOCKOUT_DURATION_MINUTES
            )
            user.status = UserStatus.LOCKED
            await _record_audit(
                session,
                user_id=user.id,
                event_type=AuthAuditEventType.ACCOUNT_LOCKED,
                ip_address=ip_address,
                user_agent=user_agent,
            )
        else:
            await _record_audit(
                session,
                user_id=user.id,
                event_type=AuthAuditEventType.LOGIN_FAILED,
                ip_address=ip_address,
                user_agent=user_agent,
                metadata={"failed_attempts": user.failed_login_attempts},
            )
        raise InvalidCredentialsError()

    # Account state checks
    if user.status == UserStatus.INACTIVE:
        raise AccountDisabledError()
    if user.status == UserStatus.ARCHIVED:
        raise AccountDisabledError()
    if user.status == UserStatus.LOCKED:
        # locked_until may have elapsed — auto-unlock if so
        if user.locked_until is None or user.locked_until <= now:
            user.status = UserStatus.ACTIVE
            user.locked_until = None
            user.failed_login_attempts = 0
            await _record_audit(
                session,
                user_id=user.id,
                event_type=AuthAuditEventType.ACCOUNT_UNLOCKED,
            )
        else:
            raise AccountLockedError()
    # INVITED means they haven't accepted the invitation yet
    if user.status == UserStatus.INVITED:
        raise EmailNotVerifiedError(details={"reason": "invitation_pending"})

    # Success — reset counters, audit, issue tokens
    user.failed_login_attempts = 0
    user.locked_until = None
    user.last_login_at = now
    # Re-hash if parameters have changed
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    await _record_audit(
        session,
        user_id=user.id,
        event_type=AuthAuditEventType.LOGIN_SUCCESS,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return await _issue_pair(
        session,
        user=user,
        ip_address=ip_address,
        user_agent=user_agent,
    )


# --------------------------------------------------------------------------
# Refresh
# --------------------------------------------------------------------------
async def refresh(
    session: AsyncSession,
    *,
    refresh_token: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> IssuedTokens:
    """Verify refresh token, check it's still active, rotate the pair."""
    payload = jwt_service.verify_refresh_token(refresh_token)

    # Set RLS context for this user so we can see their refresh_tokens rows.
    # We don't yet know the user's role — load them first then come back to
    # this if needed. For now, set user_id so the SELECT policy passes.
    from src.core.database import set_session_context

    await set_session_context(session, user_id=str(payload.user_id))

    row = (
        await session.execute(
            select(RefreshToken).where(RefreshToken.jti == payload.jti)
        )
    ).scalar_one_or_none()
    if row is None:
        raise UnauthorizedError(details={"reason": "refresh_token_unknown"})
    now = datetime.now(tz=UTC)
    if row.revoked_at is not None:
        raise UnauthorizedError(details={"reason": "refresh_token_revoked"})
    if row.expires_at <= now:
        raise UnauthorizedError(details={"reason": "refresh_token_expired"})

    user = await _load_user_with_roles(session, payload.user_id)
    if user.status in {UserStatus.INACTIVE, UserStatus.ARCHIVED}:
        raise AccountDisabledError()

    # Update the session context now that we have role + agency.
    role, agency_id = _pick_primary_role(user.roles)
    await set_session_context(
        session,
        user_id=str(payload.user_id),
        agency_id=str(agency_id) if agency_id else None,
        user_role=role.value,
    )

    # Rotate: revoke the old, issue a new pair
    row.revoked_at = now
    row.revoked_reason = "rotated"

    tokens = await _issue_pair(
        session,
        user=user,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    await _record_audit(
        session,
        user_id=user.id,
        event_type=AuthAuditEventType.TOKEN_REFRESHED,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return tokens


# --------------------------------------------------------------------------
# Logout
# --------------------------------------------------------------------------
async def logout(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    refresh_token: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Revoke a single refresh token, or all of them for the user."""
    now = datetime.now(tz=UTC)
    if refresh_token is not None:
        try:
            payload = jwt_service.verify_refresh_token(refresh_token)
        except Exception:
            # Invalid token at logout — no-op (idempotent)
            await _record_audit(
                session,
                user_id=user_id,
                event_type=AuthAuditEventType.TOKEN_REVOKED,
                ip_address=ip_address,
                user_agent=user_agent,
                metadata={"mode": "single", "result": "invalid_token"},
            )
            return
        await session.execute(
            update(RefreshToken)
            .where(
                RefreshToken.jti == payload.jti,
                RefreshToken.user_id == user_id,
            )
            .values(revoked_at=now, revoked_reason="logout")
        )
    else:
        # Logout everywhere
        await session.execute(
            update(RefreshToken)
            .where(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=now, revoked_reason="logout_all")
        )
    await _record_audit(
        session,
        user_id=user_id,
        event_type=AuthAuditEventType.TOKEN_REVOKED,
        ip_address=ip_address,
        user_agent=user_agent,
        metadata={"mode": "single" if refresh_token else "all"},
    )


# --------------------------------------------------------------------------
# Invitation tokens
# --------------------------------------------------------------------------
async def issue_invitation_token(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
) -> tuple[str, str]:
    """Issue a `SingleUseToken(purpose='invitation')` for `user_id`.

    Returns `(plaintext_token, jti)`. The token is the value the
    recipient pastes / clicks through to land on the SPA's
    `/accept-invitation?token=…` page; the `jti` is the row's primary
    key (used by `accept_invitation` to mark it consumed).

    The TTL is sourced from
    `settings.INVITATION_TOKEN_EXPIRY_DAYS` (default 7, range 1-30)
    so operators have one knob to control invitation lifetime — the
    `forgot_password` path hard-codes 2 h because password-reset is a
    more sensitive, shorter-lived flow.

    The caller is responsible for:
      1. Scheduling the invitation email (use
         `auth.email_service.send_invitation_email` with the returned
         token).
      2. Writing the `AuthAuditEventType.INVITATION_SENT` audit row
         (the staff / patients services already do this).
    """
    ttl = timedelta(days=settings.INVITATION_TOKEN_EXPIRY_DAYS)
    token, jti = jwt_service.issue_single_use_token(
        purpose="invitation",
        user_id=user_id,
        ttl=ttl,
    )
    session.add(
        SingleUseToken(
            jti=jti,
            user_id=user_id,
            purpose="invitation",
            expires_at=datetime.now(tz=UTC) + ttl,
        )
    )
    return token, jti


# --------------------------------------------------------------------------
# Accept invitation
# --------------------------------------------------------------------------
async def accept_invitation(
    session: AsyncSession,
    *,
    invitation_token: str,
    new_password: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> tuple[User, str | None]:
    """Accept an invitation, set password, issue first OTP.

    Returns (user, otp_plaintext). The caller emails the OTP — we don't
    persist the plaintext. Returns `otp_plaintext=None` if no OTP needs to be
    issued (e.g. account was already verified).
    """
    payload = jwt_service.verify_single_use_token(invitation_token, expected_purpose="invitation")
    payload = jwt_service.verify_single_use_token(invitation_token, expected_purpose="invitation")

    row = (
        await session.execute(
            select(SingleUseToken).where(SingleUseToken.jti == payload.jti)
        )
    ).scalar_one_or_none()
    now = datetime.now(tz=UTC)
    if row is None or row.consumed_at is not None or row.revoked_at is not None:
        raise InvalidInvitationTokenError()
    if row.expires_at <= now:
        row.revoked_at = now
        await _record_audit(
            session,
            user_id=row.user_id,
            event_type=AuthAuditEventType.INVITATION_EXPIRED,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        raise InvalidInvitationTokenError()

    user = await _load_user_with_roles(session, row.user_id)
    if user.status != UserStatus.INVITED:
        raise InvitationAlreadyConsumedError()

    user.password_hash = hash_password(new_password)
    user.status = UserStatus.EMAIL_VERIFICATION_PENDING
    row.consumed_at = now

    await _record_audit(
        session,
        user_id=user.id,
        event_type=AuthAuditEventType.INVITATION_ACCEPTED,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    await _record_audit(
        session,
        user_id=user.id,
        event_type=AuthAuditEventType.PASSWORD_SET,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    # Issue an OTP for the user to verify their email
    issued = await otp_service.issue_otp(
        session,
        user=user,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    await _record_audit(
        session,
        user_id=user.id,
        event_type=AuthAuditEventType.OTP_SENT,
        ip_address=ip_address,
        user_agent=user_agent,
        metadata={"via": "invitation"},
    )
    return user, issued.otp


# --------------------------------------------------------------------------
# Verify email
# --------------------------------------------------------------------------
async def verify_email(
    session: AsyncSession,
    *,
    email: str,
    otp: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> IssuedTokens:
    """Verify OTP, transition user to ACTIVE, issue first token pair."""
    result = await otp_service.verify_otp(
        session, email=email, otp=otp
    )
    user = await _load_user_with_roles(session, result.user_id)
    await _record_audit(
        session,
        user_id=user.id,
        event_type=AuthAuditEventType.OTP_VERIFIED,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    await _record_audit(
        session,
        user_id=user.id,
        event_type=AuthAuditEventType.EMAIL_VERIFIED,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return await _issue_pair(
        session,
        user=user,
        ip_address=ip_address,
        user_agent=user_agent,
    )


# --------------------------------------------------------------------------
# Resend OTP
# --------------------------------------------------------------------------
async def resend_otp(
    session: AsyncSession,
    *,
    email: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> tuple[int, otp_service.OtpIssueResult | None]:
    """Re-issue an OTP for the given email.

    Returns `(cooldown_seconds_remaining, issued_otp | None)`. The
    cooldown int is 0 when an OTP was issued now; the OtpIssueResult
    carries the plaintext OTP the caller will email. The OTP is
    `None` when the user doesn't exist (we don't leak existence — the
    caller still sees cooldown=0 and is expected to optimistically
    report "sent").

    Raises OtpResendCooldownError if the last issued OTP is too
    recent — the global handler maps that to HTTP 429 with
    `cooldown_seconds_remaining` in the body.
    """
    user = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if user is None:
        # Don't leak existence — pretend we sent it. Cooldown is 0 either way.
        return 0, None
    issued = await otp_service.resend_otp(
        session,
        user=user,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    await _record_audit(
        session,
        user_id=user.id,
        event_type=AuthAuditEventType.OTP_RESENT,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return 0, issued


# --------------------------------------------------------------------------
# Forgot password
# --------------------------------------------------------------------------
async def forgot_password(
    session: AsyncSession,
    *,
    email: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> tuple[uuid.UUID | None, str | None, str | None]:
    """Issue a password-reset single-use token for the user, if they exist.

    Returns `(user_id, email, token)` so the route can email the
    reset link. Returns `(None, None, None)` if no user matches —
    the route will still return 200 with the same shape, to avoid
    leaking account existence.

    Cooldown: if the most recent `PASSWORD_RESET_REQUESTED` audit
    event for this user is within `OTP_RESEND_COOLDOWN_SECONDS`,
    raises `OtpResendCooldownError`. We piggyback on the audit
    log so no new schema is needed.
    """
    user = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if user is None:
        return None, None, None

    # Cooldown enforcement — same window as /auth/resend-otp.
    last_audit = (
        await session.execute(
            select(AuthAuditEvent)
            .where(
                AuthAuditEvent.user_id == user.id,
                AuthAuditEvent.event_type
                == AuthAuditEventType.PASSWORD_RESET_REQUESTED,
            )
            .order_by(AuthAuditEvent.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last_audit is not None:
        cooldown_ends = last_audit.created_at + timedelta(
            seconds=settings.OTP_RESEND_COOLDOWN_SECONDS
        )
        now = datetime.now(tz=UTC)
        if cooldown_ends > now:
            remaining = int((cooldown_ends - now).total_seconds())
            raise OtpResendCooldownError(
                details={"cooldown_seconds_remaining": remaining}
            )

    ttl = timedelta(hours=2)
    token, jti = jwt_service.issue_single_use_token(
        purpose="password_reset", user_id=user.id, ttl=ttl
    )
    session.add(
        SingleUseToken(
            jti=jti,
            user_id=user.id,
            purpose="password_reset",
            expires_at=datetime.now(tz=UTC) + ttl,
        )
    )
    await _record_audit(
        session,
        user_id=user.id,
        event_type=AuthAuditEventType.PASSWORD_RESET_REQUESTED,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return user.id, user.email, token


# --------------------------------------------------------------------------
# Reset password
# --------------------------------------------------------------------------
async def reset_password(
    session: AsyncSession,
    *,
    reset_token: str,
    new_password: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Validate reset token, set new password, revoke all refresh tokens."""
    payload = jwt_service.verify_single_use_token(reset_token, expected_purpose="password_reset")
    row = (
        await session.execute(
            select(SingleUseToken).where(SingleUseToken.jti == payload.jti)
        )
    ).scalar_one_or_none()
    now = datetime.now(tz=UTC)
    if row is None or row.consumed_at is not None or row.revoked_at is not None:
        raise InvalidResetTokenError()
    if row.expires_at <= now:
        row.revoked_at = now
        raise InvalidResetTokenError()

    user = await _load_user_with_roles(session, row.user_id)
    user.password_hash = hash_password(new_password)
    user.last_password_change_at = now
    user.failed_login_attempts = 0
    user.locked_until = None
    if user.status == UserStatus.LOCKED:
        user.status = UserStatus.ACTIVE
    row.consumed_at = now

    # Revoke all outstanding refresh tokens — force re-login everywhere.
    await session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.user_id == user.id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=now, revoked_reason="password_reset")
    )

    await _record_audit(
        session,
        user_id=user.id,
        event_type=AuthAuditEventType.PASSWORD_CHANGED,
        ip_address=ip_address,
        user_agent=user_agent,
        metadata={"via": "reset"},
    )
    await _record_audit(
        session,
        user_id=user.id,
        event_type=AuthAuditEventType.PASSWORD_RESET_COMPLETED,
        ip_address=ip_address,
        user_agent=user_agent,
    )


# --------------------------------------------------------------------------
# Me
# --------------------------------------------------------------------------
async def me(session: AsyncSession, *, user_id: uuid.UUID) -> CurrentUser:
    user = await _load_user_with_roles(session, user_id)
    return _to_current_user(user)


__all__ = [
    "IssuedTokens",
    "accept_invitation",
    "forgot_password",
    "login",
    "logout",
    "me",
    "refresh",
    "resend_otp",
    "reset_password",
    "verify_email",
]
