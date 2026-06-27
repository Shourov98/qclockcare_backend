"""Auth module — request/response Pydantic schemas (DTOs).

Split into:
- `*Request` — what the client sends (validated, trimmed)
- `*Response` — what we return (excludes secrets / hashed values)

Field-level validation lives here; business validation lives in the service
layer. See `25_AUTH_AND_HOSTING_DECISIONS.md` §7 for the contract.
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

# --------------------------------------------------------------------------
# Password policy — shared by every endpoint that accepts a plaintext password
# --------------------------------------------------------------------------
_PASSWORD_MIN_LENGTH = 12
# At least one lower, one upper, one digit, one symbol (any non-alnum).
_PASSWORD_POLICY_RE = re.compile(
    r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[^A-Za-z0-9]).{12,128}$"
)


def _validate_password(value: str) -> str:
    """Enforce the QlockCare password policy (ADR-0016 §7.1)."""
    if not _PASSWORD_POLICY_RE.match(value):
        # Be deliberately vague about which rule failed to avoid leaking
        # which characters / lengths are valid.
        raise ValueError(
            "Password must be 12-128 characters and include uppercase, "
            "lowercase, a digit, and a symbol."
        )
    return value


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------
class LoginRequest(BaseModel):
    """POST /auth/login body."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class TokenPair(BaseModel):
    """Access + refresh JWT pair, plus the canonical user identity."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(
        description="Access-token lifetime in seconds (mirrors JWT_ACCESS_TOKEN_TTL_MINUTES)."
    )
    user: CurrentUser


class CurrentUser(BaseModel):
    """Identity of the authenticated user — minimal info the client needs."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    email: EmailStr
    full_name: str
    status: str
    email_verified: bool
    agency_id: str | None = Field(
        default=None,
        description="Active agency context (NULL for SUPER_ADMIN or users without a role yet).",
    )
    role: str | None = Field(
        default=None,
        description="Active role within the agency context.",
    )


# --------------------------------------------------------------------------
# Refresh
# --------------------------------------------------------------------------
class RefreshRequest(BaseModel):
    """POST /auth/refresh body."""

    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(min_length=10)


# --------------------------------------------------------------------------
# Accept invitation (step 1 of onboarding)
# --------------------------------------------------------------------------
class AcceptInvitationRequest(BaseModel):
    """POST /auth/accept-invitation body.

    `invitation_token` is the signed token emailed to the new user; we hash
    it server-side before lookup, so the plaintext never lives in the DB.
    """

    model_config = ConfigDict(extra="forbid")

    invitation_token: str = Field(min_length=20, max_length=512)
    password: str = Field(min_length=_PASSWORD_MIN_LENGTH, max_length=128)

    @field_validator("password")
    @classmethod
    def _pw_policy(cls, v: str) -> str:
        return _validate_password(v)


# --------------------------------------------------------------------------
# Verify email (step 2 of onboarding) — also used post-login until verified
# --------------------------------------------------------------------------
class VerifyEmailRequest(BaseModel):
    """POST /auth/verify-email body."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    otp: str = Field(min_length=4, max_length=8, pattern=r"^\d+$")


class VerifyEmailResponse(BaseModel):
    """What we return on successful OTP verification — a fresh token pair."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: CurrentUser


# --------------------------------------------------------------------------
# Resend OTP
# --------------------------------------------------------------------------
class ResendOtpRequest(BaseModel):
    """POST /auth/resend-otp body."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr


class ResendOtpResponse(BaseModel):
    """We never reveal whether the email exists — same shape either way."""

    sent: bool = True
    cooldown_seconds_remaining: int = 0


# --------------------------------------------------------------------------
# Password reset
# --------------------------------------------------------------------------
class ForgotPasswordRequest(BaseModel):
    """POST /auth/forgot-password body."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr


class ForgotPasswordResponse(BaseModel):
    """Always returns 'sent=True' to avoid leaking account existence."""

    sent: bool = True


class ResetPasswordRequest(BaseModel):
    """POST /auth/reset-password body."""

    model_config = ConfigDict(extra="forbid")

    reset_token: str = Field(min_length=20, max_length=512)
    password: str = Field(min_length=_PASSWORD_MIN_LENGTH, max_length=128)

    @field_validator("password")
    @classmethod
    def _pw_policy(cls, v: str) -> str:
        return _validate_password(v)


# --------------------------------------------------------------------------
# Logout
# --------------------------------------------------------------------------
class LogoutRequest(BaseModel):
    """POST /auth/logout body."""

    model_config = ConfigDict(extra="forbid")

    refresh_token: str | None = Field(
        default=None,
        description=(
            "Optional. If omitted we revoke ALL active refresh tokens for the "
            "current user (logout-everywhere)."
        ),
    )


# --------------------------------------------------------------------------
# Me
# --------------------------------------------------------------------------
class MeResponse(BaseModel):
    """GET /auth/me response — returns the current user."""

    user: CurrentUser


# --------------------------------------------------------------------------
# Error / status envelope (used by OTP failure path)
# --------------------------------------------------------------------------
class OtpAttemptStatus(BaseModel):
    """Returned by the verify endpoint on failure so clients can show the
    remaining attempts / cooldown without guessing."""

    attempts_remaining: int
    expires_at: datetime


# Forward-ref resolution for TokenPair.user
TokenPair.model_rebuild()
