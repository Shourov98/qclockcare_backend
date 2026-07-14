"""Auth module — request/response Pydantic schemas (DTOs).

Split into:
- `*Request` — what the client sends (validated, trimmed)
- `*Response` — what we return (excludes secrets / hashed values)

Field-level validation lives here; business validation lives in the service
layer. See `25_AUTH_AND_HOSTING_DECISIONS.md` §7 for the contract.

Every model carries `Field(description=...)` on each field and
`model_config(json_schema_extra={"examples": [...]})` so `/docs`
shows realistic request/response examples and Swagger UI's "Try it
out" pre-fills with sensible values.
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

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "email": "alex.rivera@careagency.com",
                    "password": "CorrectHorseBattery!42",
                }
            ]
        },
    )

    email: EmailStr = Field(
        description=(
            "Email address of an existing user. Must match the value used "
            "at invitation time (case-insensitive)."
        ),
    )
    password: str = Field(
        min_length=1,
        max_length=128,
        description=(
            "Plaintext password. Sent over TLS only — never logged. "
            "After 5 failed attempts the account is locked (ADR-0016)."
        ),
    )


class TokenPair(BaseModel):
    """Access + refresh JWT pair, plus the canonical user identity."""

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "access_token": "eyJhbGciOiJIUzI1NiIs...",
                    "refresh_token": "rt_5f3a7b1c1d0a4a239c8e1b2c3d4e5f6a",
                    "token_type": "bearer",
                    "expires_in": 900,
                    "user": {
                        "id": "5f3a7b1c-1d0a-4a23-9c8e-1b2c3d4e5f6a",
                        "email": "alex.rivera@careagency.com",
                        "full_name": "Alex Rivera",
                        "status": "ACTIVE",
                        "email_verified": True,
                        "agency_id": "8a3f12d0-7b5e-4a23-9c8e-1b2c3d4e5f6a",
                        "role": "AGENCY_ADMIN",
                    },
                }
            ]
        },
    )

    access_token: str = Field(
        description=(
            "JWT access token. Send as `Authorization: Bearer <access_token>` "
            "on every authenticated request. Lifetime is `expires_in` seconds."
        ),
    )
    refresh_token: str = Field(
        description=(
            "Opaque refresh token. Use `POST /auth/refresh` to mint a new "
            "access token without re-entering credentials. Lifetime is "
            "`settings.REFRESH_TOKEN_EXPIRY_DAYS` days."
        ),
    )
    token_type: str = Field(
        default="bearer",
        description="Always `bearer`. Present for OAuth 2.0 compatibility.",
    )
    expires_in: int = Field(
        description=(
            "Access-token lifetime in seconds (mirrors "
            "`settings.ACCESS_TOKEN_EXPIRY_MINUTES`)."
        ),
    )
    user: CurrentUser = Field(
        description="Canonical identity of the authenticated user.",
    )


class CurrentUser(BaseModel):
    """Identity of the authenticated user — minimal info the client needs."""

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "id": "5f3a7b1c-1d0a-4a23-9c8e-1b2c3d4e5f6a",
                    "email": "alex.rivera@careagency.com",
                    "full_name": "Alex Rivera",
                    "status": "ACTIVE",
                    "email_verified": True,
                    "agency_id": "8a3f12d0-7b5e-4a23-9c8e-1b2c3d4e5f6a",
                    "role": "AGENCY_ADMIN",
                }
            ]
        },
    )

    id: str = Field(description="Stable user UUID.")
    email: EmailStr = Field(description="Verified email address.")
    full_name: str = Field(description="Display name (first + last).")
    status: str = Field(
        description=(
            "Lifecycle status (`INVITED`, `EMAIL_VERIFICATION_PENDING`, "
            "`ACTIVE`, `INACTIVE`, `LOCKED`, `ARCHIVED`)."
        ),
    )
    email_verified: bool = Field(
        description="`true` once the OTP verification step has completed.",
    )
    agency_id: str | None = Field(
        default=None,
        description=(
            "Active agency context (NULL for `SUPER_ADMIN` or users "
            "without a role yet). RLS policies scope queries to this ID."
        ),
    )
    role: str | None = Field(
        default=None,
        description=(
            "Active role within the agency context "
            "(`SUPER_ADMIN`, `AGENCY_ADMIN`, `STAFF`, `PATIENT`, `GUARDIAN`)."
        ),
    )


# --------------------------------------------------------------------------
# Refresh
# --------------------------------------------------------------------------
class RefreshRequest(BaseModel):
    """POST /auth/refresh body."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"refresh_token": "rt_5f3a7b1c1d0a4a239c8e1b2c3d4e5f6a"}
            ]
        },
    )

    refresh_token: str = Field(
        min_length=10,
        description=(
            "Refresh token issued by `POST /auth/login` (or by a previous "
            "refresh). Rotated on every successful refresh — store the new "
            "value and discard the old one."
        ),
    )


# --------------------------------------------------------------------------
# Accept invitation (step 1 of onboarding)
# --------------------------------------------------------------------------
class AcceptInvitationRequest(BaseModel):
    """POST /auth/accept-invitation body.

    `invitation_token` is the signed token emailed to the new user; we hash
    it server-side before lookup, so the plaintext never lives in the DB.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "invitation_token": "inv_5f3a7b1c1d0a4a239c8e1b2c3d4e5f6a",
                    "password": "CorrectHorseBattery!42",
                }
            ]
        },
    )

    invitation_token: str = Field(
        min_length=20,
        max_length=512,
        description=(
            "Token from the invitation email's deep link "
            "(`/accept-invitation?token=...`). Single-use; expires after "
            "`settings.INVITATION_TOKEN_EXPIRY_DAYS` days."
        ),
    )
    password: str = Field(
        min_length=_PASSWORD_MIN_LENGTH,
        max_length=128,
        description=(
            "New password chosen by the invitee. Must satisfy the project "
            "password policy (12-128 chars, mixed case + digit + symbol)."
        ),
    )

    @field_validator("password")
    @classmethod
    def _pw_policy(cls, v: str) -> str:
        return _validate_password(v)


# --------------------------------------------------------------------------
# Verify email (step 2 of onboarding) — also used post-login until verified
# --------------------------------------------------------------------------
class VerifyEmailRequest(BaseModel):
    """POST /auth/verify-email body."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "email": "alex.rivera@careagency.com",
                    "otp": "482915",
                }
            ]
        },
    )

    email: EmailStr = Field(
        description="Email address the OTP was sent to.",
    )
    otp: str = Field(
        min_length=4,
        max_length=8,
        pattern=r"^\d+$",
        description=(
            "6-digit verification code from the welcome email. "
            "Expires after `settings.OTP_EXPIRY_MINUTES` minutes; "
            "max 5 attempts before the account is locked."
        ),
    )


class VerifyEmailResponse(BaseModel):
    """What we return on successful OTP verification — a fresh token pair."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "access_token": "eyJhbGciOiJIUzI1NiIs...",
                    "refresh_token": "rt_5f3a7b1c1d0a4a239c8e1b2c3d4e5f6a",
                    "token_type": "bearer",
                    "expires_in": 900,
                    "user": {
                        "id": "5f3a7b1c-1d0a-4a23-9c8e-1b2c3d4e5f6a",
                        "email": "alex.rivera@careagency.com",
                        "full_name": "Alex Rivera",
                        "status": "ACTIVE",
                        "email_verified": True,
                        "agency_id": "8a3f12d0-7b5e-4a23-9c8e-1b2c3d4e5f6a",
                        "role": "STAFF",
                    },
                }
            ]
        },
    )

    access_token: str = Field(description="Fresh access token (see `TokenPair`).")
    refresh_token: str = Field(description="Fresh refresh token.")
    token_type: str = Field(
        default="bearer",
        description="Always `bearer`.",
    )
    expires_in: int = Field(description="Access-token lifetime in seconds.")
    user: CurrentUser = Field(description="Identity of the verified user.")


# --------------------------------------------------------------------------
# Resend OTP
# --------------------------------------------------------------------------
class ResendOtpRequest(BaseModel):
    """POST /auth/resend-otp body."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [{"email": "alex.rivera@careagency.com"}]
        },
    )

    email: EmailStr = Field(
        description=(
            "Email of the account that needs a fresh OTP. Rate-limited — "
            "wait at least `cooldown_seconds_remaining` seconds between "
            "requests."
        ),
    )


class ResendOtpResponse(BaseModel):
    """We never reveal whether the email exists — same shape either way."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"sent": True, "cooldown_seconds_remaining": 0}
            ]
        },
    )

    sent: bool = Field(
        default=True,
        description=(
            "Always `true`. We don't reveal whether the email exists to "
            "avoid leaking account presence."
        ),
    )
    cooldown_seconds_remaining: int = Field(
        default=0,
        description=(
            "Seconds to wait before the next resend will be accepted. "
            "Zero means a fresh OTP can be requested right away."
        ),
    )


# --------------------------------------------------------------------------
# Password reset
# --------------------------------------------------------------------------
class ForgotPasswordRequest(BaseModel):
    """POST /auth/forgot-password body."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [{"email": "alex.rivera@careagency.com"}]
        },
    )

    email: EmailStr = Field(
        description=(
            "Email of the account that needs a password reset. If the email "
            "exists, a reset link is sent — otherwise the response is "
            "indistinguishable (no account-existence leak)."
        ),
    )


class ForgotPasswordResponse(BaseModel):
    """Always returns 'sent=True' to avoid leaking account existence."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"sent": True}]}
    )

    sent: bool = Field(
        default=True,
        description="Always `true`. The reset email is sent if the account exists.",
    )


class ResetPasswordRequest(BaseModel):
    """POST /auth/reset-password body."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "reset_token": "rst_5f3a7b1c1d0a4a239c8e1b2c3d4e5f6a",
                    "password": "CorrectHorseBattery!42",
                }
            ]
        },
    )

    reset_token: str = Field(
        min_length=20,
        max_length=512,
        description=(
            "Token from the password-reset email's deep link "
            "(`/reset-password?token=...`). Single-use; expires after "
            "`settings.PASSWORD_RESET_TOKEN_EXPIRY_MINUTES` minutes."
        ),
    )
    password: str = Field(
        min_length=_PASSWORD_MIN_LENGTH,
        max_length=128,
        description="New password. Must satisfy the project password policy.",
    )

    @field_validator("password")
    @classmethod
    def _pw_policy(cls, v: str) -> str:
        return _validate_password(v)


# --------------------------------------------------------------------------
# Logout
# --------------------------------------------------------------------------
class LogoutRequest(BaseModel):
    """POST /auth/logout body."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"refresh_token": "rt_5f3a7b1c1d0a4a239c8e1b2c3d4e5f6a"},
                {"refresh_token": None},
            ]
        },
    )

    refresh_token: str | None = Field(
        default=None,
        description=(
            "Optional. If omitted we revoke ALL active refresh tokens for the "
            "current user (logout-everywhere). Otherwise only the supplied "
            "refresh token is revoked."
        ),
    )


# --------------------------------------------------------------------------
# Me
# --------------------------------------------------------------------------
class MeResponse(BaseModel):
    """GET /auth/me response — returns the current user."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "user": {
                        "id": "5f3a7b1c-1d0a-4a23-9c8e-1b2c3d4e5f6a",
                        "email": "alex.rivera@careagency.com",
                        "full_name": "Alex Rivera",
                        "status": "ACTIVE",
                        "email_verified": True,
                        "agency_id": "8a3f12d0-7b5e-4a23-9c8e-1b2c3d4e5f6a",
                        "role": "AGENCY_ADMIN",
                    }
                }
            ]
        },
    )

    user: CurrentUser = Field(
        description="The currently authenticated user (from the bearer token).",
    )


# --------------------------------------------------------------------------
# Error / status envelope (used by OTP failure path)
# --------------------------------------------------------------------------
class OtpAttemptStatus(BaseModel):
    """Returned by the verify endpoint on failure so clients can show the
    remaining attempts / cooldown without guessing."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "attempts_remaining": 3,
                    "expires_at": "2026-06-28T10:33:00Z",
                }
            ]
        },
    )

    attempts_remaining: int = Field(
        description=(
            "OTP attempts left before the account is locked. Decrements on "
            "each failed verify; resets on a successful verify or a resend."
        ),
    )
    expires_at: datetime = Field(
        description="UTC ISO-8601 timestamp when the current OTP expires.",
    )


# Forward-ref resolution for TokenPair.user
TokenPair.model_rebuild()

__all__ = [
    "AcceptInvitationRequest",
    "CurrentUser",
    "ForgotPasswordRequest",
    "ForgotPasswordResponse",
    "LoginRequest",
    "LogoutRequest",
    "MeResponse",
    "OtpAttemptStatus",
    "RefreshRequest",
    "ResendOtpRequest",
    "ResendOtpResponse",
    "ResetPasswordRequest",
    "TokenPair",
    "VerifyEmailRequest",
    "VerifyEmailResponse",
]
