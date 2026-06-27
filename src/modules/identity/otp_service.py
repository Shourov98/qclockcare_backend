"""OTP service — issue, verify, and rate-limit email-verification OTPs.

Flow (ADR-0016 §7.4):
1.  `issue_otp(user)` — generate a fresh OTP, hash it with argon2, insert a
    row in `email_verification_otps`. Returns the plaintext OTP (caller emails
    it). Any previous unconsumed OTP for the same user is marked consumed.
2.  `verify_otp(email, otp)` — verify the OTP against the latest unconsumed,
    unexpired row for that email. Increments `attempts` on failure. Locks
    the user account after `OTP_MAX_ATTEMPTS` incorrect attempts.
3.  `resend_otp(email)` — enforce a cooldown, then re-issue.

All side effects (account state changes, audit events) are persisted via the
caller's session — pass `session` explicitly.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.exceptions import (
    InvalidOtpError,
    OtpExpiredError,
    OtpMaxAttemptsExceededError,
    OtpResendCooldownError,
)
from src.core.logging import get_logger
from src.core.security import hash_password, verify_password
from src.modules.identity.models import EmailVerificationOtp, User
from src.modules.identity.password_utils import generate_otp

logger = get_logger(__name__)


# --------------------------------------------------------------------------
# DTOs
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class OtpIssueResult:
    user_id: uuid.UUID
    email: str
    full_name: str | None
    otp: str  # plaintext, returned for the email send step
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class OtpVerifyResult:
    user_id: uuid.UUID
    email: str


# --------------------------------------------------------------------------
# Issue
# --------------------------------------------------------------------------
async def issue_otp(
    session: AsyncSession,
    *,
    user: User,
    ip_address: str | None = None,
    user_agent: str | None = None,
    is_resend: bool = False,
) -> OtpIssueResult:
    """Generate, hash, persist a fresh OTP for `user`.

    Any prior unconsumed OTP for this user is marked consumed (we only keep
    one outstanding code at a time).
    """
    now = datetime.now(tz=UTC)
    otp = generate_otp(settings.OTP_LENGTH)
    otp_hash = hash_password(otp)
    expires_at = now + timedelta(minutes=settings.OTP_EXPIRY_MINUTES)

    # Consume any prior unconsumed OTPs for this user
    await session.execute(
        update(EmailVerificationOtp)
        .where(
            EmailVerificationOtp.user_id == user.id,
            EmailVerificationOtp.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )

    # Insert the new OTP
    row = EmailVerificationOtp(
        user_id=user.id,
        email=user.email,
        otp_hash=otp_hash,
        expires_at=expires_at,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    session.add(row)
    await session.flush()  # populate row.id + row.created_at

    logger.info(
        "otp.issued",
        user_id=str(user.id),
        is_resend=is_resend,
        expires_at=expires_at.isoformat(),
    )
    return OtpIssueResult(
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        otp=otp,
        expires_at=expires_at,
    )


# --------------------------------------------------------------------------
# Verify
# --------------------------------------------------------------------------
async def verify_otp(
    session: AsyncSession,
    *,
    email: str,
    otp: str,
) -> OtpVerifyResult:
    """Verify an OTP against the latest unconsumed row for `email`.

    On success:
      - Marks the row consumed.
      - Transitions user.status → ACTIVE (if it was EMAIL_VERIFICATION_PENDING).
      - Sets user.email_verified_at = now().

    On failure:
      - Increments `attempts`. After `OTP_MAX_ATTEMPTS`, raises
        OtpMaxAttemptsExceededError and consumes the row.
      - If the row is past `expires_at`, raises OtpExpiredError.
    """
    now = datetime.now(tz=UTC)
    stmt = (
        select(EmailVerificationOtp)
        .where(
            EmailVerificationOtp.email == email,
            EmailVerificationOtp.consumed_at.is_(None),
        )
        .order_by(EmailVerificationOtp.created_at.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        # No active OTP — treat as expired
        raise OtpExpiredError()

    # Find the user too — needed to set status on success
    user = (
        await session.execute(select(User).where(User.id == row.user_id))
    ).scalar_one_or_none()
    if user is None:
        # Orphan row — treat as expired
        raise OtpExpiredError()

    if row.expires_at <= now:
        row.consumed_at = now
        await session.flush()
        raise OtpExpiredError()

    if row.attempts >= settings.OTP_MAX_ATTEMPTS:
        row.consumed_at = now
        await session.flush()
        raise OtpMaxAttemptsExceededError(
            details={"attempts": row.attempts, "max_attempts": settings.OTP_MAX_ATTEMPTS}
        )

    # argon2 verify — uses our wrapper to swallow exceptions and return bool
    if not verify_password(otp, row.otp_hash):
        row.attempts += 1
        # If this attempt pushed us over the limit, consume the row
        if row.attempts >= settings.OTP_MAX_ATTEMPTS:
            row.consumed_at = now
        await session.flush()
        attempts_remaining = max(0, settings.OTP_MAX_ATTEMPTS - row.attempts)
        raise InvalidOtpError(
            details={"attempts_remaining": attempts_remaining}
        )

    # Success path
    row.consumed_at = now
    if user.email_verified_at is None:
        user.email_verified_at = now
    if user.status.value in {"EMAIL_VERIFICATION_PENDING", "INVITED"}:
        from src.shared.domain.enums import UserStatus

        user.status = UserStatus.ACTIVE
    await session.flush()

    logger.info(
        "otp.verified",
        user_id=str(user.id),
        email=user.email,
    )
    return OtpVerifyResult(user_id=user.id, email=user.email)


# --------------------------------------------------------------------------
# Resend
# --------------------------------------------------------------------------
async def resend_otp(
    session: AsyncSession,
    *,
    user: User,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> OtpIssueResult:
    """Re-issue an OTP, enforcing the cooldown window.

    Raises OtpResendCooldownError if the last issued OTP is too recent.
    """
    now = datetime.now(tz=UTC)
    stmt = (
        select(EmailVerificationOtp)
        .where(EmailVerificationOtp.user_id == user.id)
        .order_by(EmailVerificationOtp.created_at.desc())
        .limit(1)
    )
    last = (await session.execute(stmt)).scalar_one_or_none()
    if last is not None:
        cooldown_ends = last.created_at + timedelta(
            seconds=settings.OTP_RESEND_COOLDOWN_SECONDS
        )
        if cooldown_ends > now:
            remaining = int((cooldown_ends - now).total_seconds())
            raise OtpResendCooldownError(
                details={"cooldown_seconds_remaining": remaining}
            )
    return await issue_otp(
        session,
        user=user,
        ip_address=ip_address,
        user_agent=user_agent,
        is_resend=True,
    )


__all__ = [
    "OtpIssueResult",
    "OtpVerifyResult",
    "issue_otp",
    "resend_otp",
    "verify_otp",
]
