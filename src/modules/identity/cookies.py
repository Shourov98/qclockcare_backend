"""HttpOnly auth cookies + CSRF support (double-submit cookie pattern).

This module is the single source of truth for cookie attributes on
`qc_access`, `qc_refresh`, and `qc_csrf`. Routers and dependencies
should never set cookies directly — they should call `set_auth_cookies`
or `clear_auth_cookies` from here so the attribute set stays consistent.

Cookie names:
    qc_access   — short-lived JWT (HttpOnly, used by the API for auth)
    qc_refresh  — long-lived opaque token (HttpOnly, scoped to /auth/*)
    qc_csrf     — random 256-bit token, NOT HttpOnly (the SPA reads it
                  and echoes it back in the `X-CSRF-Token` header on
                  every unsafe request)

CSRF model (double-submit):
    1. On login / refresh we mint a fresh CSRF token and store it as a
       non-HttpOnly cookie. The SPA reads it via `document.cookie` and
       keeps it in memory (no second round trip needed).
    2. On every unsafe request (POST/PUT/PATCH/DELETE), the SPA echoes
       the token in the `X-CSRF-Token` header.
    3. The `csrf_protect` dependency compares the header to the cookie
       using `hmac.compare_digest`. Bearer clients (Authorization
       header) bypass this check — they don't share a cookie jar with
       the browser, so requiring CSRF would break legitimate API
       clients (curl, server-to-server, mobile apps) that use bearer
       tokens exclusively.

Path scoping:
    `qc_refresh` is scoped to `/auth/*` because it's only ever sent to
    the refresh / logout endpoints. This shrinks the blast radius if
    the cookie is ever exfiltrated from a non-auth endpoint.
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import Request
from starlette.responses import Response

from src.core.config import settings
from src.core.exceptions import ForbiddenError

# --------------------------------------------------------------------------
# Cookie names — public constants used by tests and clients.
# --------------------------------------------------------------------------
QC_ACCESS_COOKIE: str = "qc_access"
QC_REFRESH_COOKIE: str = "qc_refresh"
QC_CSRF_COOKIE: str = "qc_csrf"

# Methods that can mutate state — CSRF only applies to these.
_UNSAFE_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# --------------------------------------------------------------------------
# Public cookie helpers
# --------------------------------------------------------------------------
def set_auth_cookies(
    response: Response,
    access_token: str,
    refresh_token: str,
    expires_in: int,
) -> str:
    """Set `qc_access`, `qc_refresh`, and `qc_csrf` on the response.

    Returns the freshly generated CSRF token. The SPA picks the cookie
    value up from `document.cookie`; the returned string is exposed for
    callers that want to seed an in-memory copy (e.g. for tests, or for
    backend-to-backend priming of an SPA).
    """
    csrf_token = secrets.token_urlsafe(32)

    common: dict[str, object] = {
        "secure": settings.COOKIE_SECURE,
        "samesite": settings.COOKIE_SAMESITE,
        "domain": settings.COOKIE_DOMAIN,
        "path": "/",
    }

    response.set_cookie(
        key=QC_ACCESS_COOKIE,
        value=access_token,
        max_age=expires_in,
        httponly=True,
        **common,
    )
    response.set_cookie(
        key=QC_REFRESH_COOKIE,
        value=refresh_token,
        # Refresh tokens are only used by /auth/refresh + /auth/logout,
        # so we narrow the path to /auth to limit CSRF / XSS exposure
        # on the rest of the surface.
        path="/auth",
        max_age=settings.JWT_REFRESH_TOKEN_TTL_DAYS * 86400,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
        domain=settings.COOKIE_DOMAIN,
    )
    response.set_cookie(
        key=QC_CSRF_COOKIE,
        value=csrf_token,
        # CSRF cookie is deliberately NOT HttpOnly — the SPA needs to
        # read it so it can echo the value in the X-CSRF-Token header.
        httponly=False,
        **common,
    )

    return csrf_token


def clear_auth_cookies(response: Response) -> None:
    """Remove every QlockCare auth cookie from the client.

    Must match the path / domain used at issue time — otherwise the
    browser scopes the delete to a different (Path, Domain) tuple and
    the original cookie stays around.
    """
    common: dict[str, object] = {
        "secure": settings.COOKIE_SECURE,
        "samesite": settings.COOKIE_SAMESITE,
        "domain": settings.COOKIE_DOMAIN,
    }
    response.set_cookie(
        key=QC_ACCESS_COOKIE,
        value="",
        max_age=0,
        path="/",
        httponly=True,
        **common,
    )
    response.set_cookie(
        key=QC_REFRESH_COOKIE,
        value="",
        max_age=0,
        path="/auth",
        httponly=True,
        **common,
    )
    response.set_cookie(
        key=QC_CSRF_COOKIE,
        value="",
        max_age=0,
        path="/",
        httponly=False,
        **common,
    )


# --------------------------------------------------------------------------
# CSRF protection dependency
# --------------------------------------------------------------------------
def csrf_protect(request: Request) -> None:
    """FastAPI dependency — verify the CSRF header against the cookie.

    Safe methods (GET/HEAD/OPTIONS) pass through. Bearer requests
    bypass the check unconditionally because bearer clients don't
    share the browser's cookie jar. Cookie-auth requests must echo
    the `qc_csrf` cookie value in the `X-CSRF-Token` header.

    Raises `ForbiddenError` (403) on mismatch / missing value, routed
    through the project's global error envelope.
    """
    if request.method not in _UNSAFE_METHODS:
        return

    # Bearer clients (Authorization header) bypass CSRF — they have no
    # cookie to echo. This keeps the API friendly to curl / mobile /
    # server-to-server callers that don't have a same-origin cookie jar.
    auth_header = request.headers.get("authorization") or ""
    if auth_header.lower().startswith("bearer "):
        return

    cookie_token = request.cookies.get(QC_CSRF_COOKIE)
    header_token = request.headers.get("x-csrf-token")

    if not cookie_token or not header_token:
        raise ForbiddenError(
            message="CSRF token missing.",
            details={"reason": "missing_cookie_or_header"},
        )
    # Constant-time compare so a malicious site can't probe the cookie
    # value byte-by-byte via timing differences.
    if not hmac.compare_digest(cookie_token, header_token):
        raise ForbiddenError(
            message="CSRF token mismatch.",
            details={"reason": "header_does_not_match_cookie"},
        )


__all__ = [
    "QC_ACCESS_COOKIE",
    "QC_CSRF_COOKIE",
    "QC_REFRESH_COOKIE",
    "clear_auth_cookies",
    "csrf_protect",
    "set_auth_cookies",
]
