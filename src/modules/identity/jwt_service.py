"""JWT service — issue and verify access + refresh tokens (ADR-0016 §7.2).

The token format is JWT (RFC 7519) with HS256 in dev / RS256 in prod.

Claims layout:

  Access token (short-lived, ~15 min):
    sub              — user UUID
    email            — user email (cheap client display)
    role             — current role (defaults to highest-privilege role)
    agency_id        — current agency context (NULL for SUPER_ADMIN / no role)
    agency_role      — alias for `role`, kept for clarity in policies
    typ              — "access"
    iss / aud / iat / exp / jti

  Refresh token (long-lived, ~7 days):
    sub              — user UUID
    typ              — "refresh"
    jti              — unique id, used as the row key in refresh_tokens
    iss / aud / iat / exp

The refresh-token table tracks jti → user_id so we can revoke individual
tokens (logout) or all tokens for a user (logout-everywhere, password change).
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from src.core.config import settings
from src.core.exceptions import TokenExpiredError, TokenInvalidError


# --------------------------------------------------------------------------
# Types
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AccessTokenPayload:
    """The verified claims of an access token."""

    user_id: uuid.UUID
    email: str
    role: str
    agency_id: uuid.UUID | None
    jti: str


@dataclass(frozen=True, slots=True)
class RefreshTokenPayload:
    """The verified claims of a refresh token."""

    user_id: uuid.UUID
    jti: str


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(tz=UTC)


def _encode(payload: Mapping[str, Any]) -> str:
    if settings.JWT_ALGORITHM == "HS256":
        key = settings.SUPABASE_JWT_SECRET.get_secret_value()
        if not key:
            raise TokenInvalidError(details={"reason": "JWT secret not configured"})
    elif settings.JWT_ALGORITHM == "RS256":
        key = settings.JWT_PRIVATE_KEY.get_secret_value()  # type: ignore[union-attr]
        if not key:
            raise TokenInvalidError(details={"reason": "JWT private key not configured"})
    else:  # pragma: no cover — guarded by Settings Literal
        raise TokenInvalidError(details={"reason": f"unsupported alg {settings.JWT_ALGORITHM}"})
    return jwt.encode(dict(payload), key, algorithm=settings.JWT_ALGORITHM)


def _decode(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(
            token,
            # Verify against the right key per algorithm.
            (
                settings.SUPABASE_JWT_SECRET.get_secret_value()
                if settings.JWT_ALGORITHM == "HS256"
                else settings.JWT_PUBLIC_KEY.get_secret_value()  # type: ignore[union-attr]
            ),
            algorithms=[settings.JWT_ALGORITHM],
            audience=settings.JWT_AUDIENCE,
            issuer=settings.JWT_ISSUER,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError() from exc
    except jwt.InvalidTokenError as exc:
        raise TokenInvalidError(details={"reason": str(exc)}) from exc


# --------------------------------------------------------------------------
# Issue
# --------------------------------------------------------------------------
def issue_access_token(
    *,
    user_id: uuid.UUID,
    email: str,
    role: str,
    agency_id: uuid.UUID | None,
) -> tuple[str, int]:
    """Issue a short-lived access token. Returns (token, expires_in_seconds)."""
    now = _now()
    ttl = timedelta(minutes=settings.JWT_ACCESS_TOKEN_TTL_MINUTES)
    jti = secrets.token_urlsafe(16)
    payload = {
        "sub": str(user_id),
        "email": email,
        "role": role,
        "agency_id": str(agency_id) if agency_id is not None else None,
        "agency_role": role,
        "typ": "access",
        "jti": jti,
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
    }
    return _encode(payload), int(ttl.total_seconds())


def issue_refresh_token(*, user_id: uuid.UUID) -> tuple[str, str, datetime]:
    """Issue a refresh token. Returns (token, jti, expires_at)."""
    now = _now()
    ttl = timedelta(days=settings.JWT_REFRESH_TOKEN_TTL_DAYS)
    jti = secrets.token_urlsafe(24)
    payload = {
        "sub": str(user_id),
        "typ": "refresh",
        "jti": jti,
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
    }
    return _encode(payload), jti, now + ttl


# --------------------------------------------------------------------------
# Verify
# --------------------------------------------------------------------------
def verify_access_token(token: str) -> AccessTokenPayload:
    """Verify an access token and return its typed claims."""
    claims = _decode(token)
    if claims.get("typ") != "access":
        raise TokenInvalidError(details={"reason": "expected access token"})
    return AccessTokenPayload(
        user_id=uuid.UUID(claims["sub"]),
        email=claims.get("email", ""),
        role=claims.get("role", ""),
        agency_id=uuid.UUID(claims["agency_id"]) if claims.get("agency_id") else None,
        jti=claims["jti"],
    )


def verify_refresh_token(token: str) -> RefreshTokenPayload:
    """Verify a refresh token and return its typed claims."""
    claims = _decode(token)
    if claims.get("typ") != "refresh":
        raise TokenInvalidError(details={"reason": "expected refresh token"})
    return RefreshTokenPayload(
        user_id=uuid.UUID(claims["sub"]),
        jti=claims["jti"],
    )


# --------------------------------------------------------------------------
# Generic tokens (invitation, password reset)
# --------------------------------------------------------------------------
def issue_single_use_token(
    *,
    purpose: str,
    user_id: uuid.UUID,
    ttl: timedelta,
    extra: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    """Issue a single-use, purpose-scoped token (e.g. invitation, reset).

    Returns (token, jti). The jti is the row key in `single_use_tokens`,
    so revocation = "delete the row".
    """
    now = _now()
    jti = secrets.token_urlsafe(24)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "purpose": purpose,
        "typ": "single_use",
        "jti": jti,
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
    }
    if extra:
        payload.update(extra)
    return _encode(payload), jti


@dataclass(frozen=True, slots=True)
class SingleUseTokenPayload:
    user_id: uuid.UUID
    purpose: str
    jti: str
    claims: Mapping[str, Any]


def verify_single_use_token(token: str, *, expected_purpose: str) -> SingleUseTokenPayload:
    """Verify a single-use token; raises TokenInvalidError if purpose mismatches."""
    claims = _decode(token)
    if claims.get("typ") != "single_use":
        raise TokenInvalidError(details={"reason": "expected single-use token"})
    if claims.get("purpose") != expected_purpose:
        raise TokenInvalidError(details={"reason": f"expected purpose {expected_purpose}"})
    return SingleUseTokenPayload(
        user_id=uuid.UUID(claims["sub"]),
        purpose=claims["purpose"],
        jti=claims["jti"],
        claims=claims,
    )


__all__ = [
    "AccessTokenPayload",
    "RefreshTokenPayload",
    "SingleUseTokenPayload",
    "issue_access_token",
    "issue_refresh_token",
    "issue_single_use_token",
    "verify_access_token",
    "verify_refresh_token",
    "verify_single_use_token",
]