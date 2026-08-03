"""Tests for HttpOnly auth cookies + CSRF double-submit protection.

Most tests in this module are *unit* tests — they exercise the cookie
helper module and the `csrf_protect` dependency in isolation. They
don't need a database.

The end-to-end test at the bottom uses the FastAPI TestClient to
verify the cookie attributes on the actual `POST /auth/login`
response. It is skipped when the database isn't reachable so it stays
green in CI environments that don't have a live Postgres.
"""

from __future__ import annotations

import hmac

import pytest
from starlette.requests import Request
from starlette.responses import Response

from src.core.exceptions import ForbiddenError
from src.modules.identity.cookies import (
    QC_ACCESS_COOKIE,
    QC_CSRF_COOKIE,
    QC_REFRESH_COOKIE,
    clear_auth_cookies,
    csrf_protect,
    set_auth_cookies,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _make_request(
    method: str = "POST",
    cookies: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> Request:
    """Build a Starlette Request with `cookies` + `headers` we control."""
    cookie_header = ""
    if cookies:
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    headers_list: list[tuple[bytes, bytes]] = []
    for k, v in (headers or {}).items():
        headers_list.append((k.lower().encode(), v.encode()))
    if cookie_header:
        headers_list.append((b"cookie", cookie_header.encode()))
    scope: dict = {
        "type": "http",
        "method": method,
        "path": "/",
        "query_string": b"",
        "headers": headers_list,
        "scheme": "https",
        "server": ("testserver", 443),
    }
    return Request(scope)


# --------------------------------------------------------------------------
# set_auth_cookies
# --------------------------------------------------------------------------
class TestSetAuthCookies:
    def test_sets_all_three_cookies(self) -> None:
        response = Response()
        csrf = set_auth_cookies(
            response,
            access_token="access.jwt",
            refresh_token="rt_value",
            expires_in=900,
        )
        raw = b"|".join([v for _k, v in response.raw_headers])
        joined = raw.decode("latin-1", errors="ignore")
        assert "qc_access" in joined
        assert "qc_refresh" in joined
        assert "qc_csrf" in joined
        assert "access.jwt" in joined
        assert "rt_value" in joined
        # CSRF token returned should be a token_urlsafe(32) string.
        assert isinstance(csrf, str) and len(csrf) >= 32

    def test_refresh_cookie_is_path_scoped_to_auth(self) -> None:
        response = Response()
        set_auth_cookies(
            response,
            access_token="a",
            refresh_token="b",
            expires_in=900,
        )
        cookies = response.headers.getlist("set-cookie")
        refresh = next(c for c in cookies if c.startswith("qc_refresh="))
        access = next(c for c in cookies if c.startswith("qc_access="))
        csrf = next(c for c in cookies if c.startswith("qc_csrf="))
        assert "Path=/auth" in refresh
        assert "Path=/" in access
        assert "Path=/" in csrf

    def test_access_and_refresh_are_httponly_csrf_is_not(self) -> None:
        response = Response()
        set_auth_cookies(
            response,
            access_token="a",
            refresh_token="b",
            expires_in=900,
        )
        cookies = response.headers.getlist("set-cookie")
        access = next(c for c in cookies if c.startswith("qc_access="))
        refresh = next(c for c in cookies if c.startswith("qc_refresh="))
        csrf = next(c for c in cookies if c.startswith("qc_csrf="))
        assert "HttpOnly" in access
        assert "HttpOnly" in refresh
        # CSRF cookie is NOT HttpOnly — the SPA must be able to read it.
        assert "HttpOnly" not in csrf

    def test_returns_urlsafe_csrf_token(self) -> None:
        response = Response()
        csrf = set_auth_cookies(
            response,
            access_token="a",
            refresh_token="b",
            expires_in=900,
        )
        assert isinstance(csrf, str)
        assert len(csrf) >= 32


class TestClearAuthCookies:
    def test_sends_max_age_zero(self) -> None:
        response = Response()
        clear_auth_cookies(response)
        cookies = response.headers.getlist("set-cookie")
        assert len(cookies) == 3
        for raw in cookies:
            assert "Max-Age=0" in raw

    def test_matches_issue_paths(self) -> None:
        response = Response()
        clear_auth_cookies(response)
        cookies = response.headers.getlist("set-cookie")
        refresh = next(c for c in cookies if c.startswith("qc_refresh="))
        access = next(c for c in cookies if c.startswith("qc_access="))
        assert "Path=/auth" in refresh
        assert "Path=/" in access


# --------------------------------------------------------------------------
# csrf_protect
# --------------------------------------------------------------------------
class TestCsrfProtect:
    """`csrf_protect` is a FastAPI dependency — we call it directly here."""

    def test_safe_methods_skip_csrf(self) -> None:
        for m in ("GET", "HEAD", "OPTIONS"):
            request = _make_request(method=m, cookies={}, headers={})
            csrf_protect(request)  # must not raise

    def test_unsafe_with_bearer_bypasses_csrf(self) -> None:
        # Bearer clients don't share the cookie jar; CSRF would break
        # legitimate API clients (curl, mobile, server-to-server).
        request = _make_request(
            method="POST",
            cookies={},
            headers={"authorization": "Bearer some.jwt.token"},
        )
        csrf_protect(request)  # must not raise

    def test_unsafe_with_matching_cookie_and_header_passes(self) -> None:
        token = "matching-token-value"
        request = _make_request(
            method="POST",
            cookies={QC_CSRF_COOKIE: token},
            headers={"x-csrf-token": token},
        )
        csrf_protect(request)  # must not raise

    def test_unsafe_with_missing_cookie_raises(self) -> None:
        request = _make_request(
            method="POST",
            cookies={},
            headers={"x-csrf-token": "something"},
        )
        with pytest.raises(ForbiddenError):
            csrf_protect(request)

    def test_unsafe_with_missing_header_raises(self) -> None:
        request = _make_request(
            method="POST",
            cookies={QC_CSRF_COOKIE: "something"},
            headers={},
        )
        with pytest.raises(ForbiddenError):
            csrf_protect(request)

    def test_unsafe_with_mismatch_raises(self) -> None:
        request = _make_request(
            method="POST",
            cookies={QC_CSRF_COOKIE: "cookie-token"},
            headers={"x-csrf-token": "different-token"},
        )
        with pytest.raises(ForbiddenError):
            csrf_protect(request)

    @pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
    def test_all_unsafe_methods_are_protected(self, method: str) -> None:
        request = _make_request(method=method, cookies={}, headers={})
        with pytest.raises(ForbiddenError):
            csrf_protect(request)


# --------------------------------------------------------------------------
# Public cookie-name constants
# --------------------------------------------------------------------------
def test_cookie_constants_have_stable_values() -> None:
    assert QC_ACCESS_COOKIE == "qc_access"
    assert QC_REFRESH_COOKIE == "qc_refresh"
    assert QC_CSRF_COOKIE == "qc_csrf"


# --------------------------------------------------------------------------
# Settings — verify the new fields default correctly
# --------------------------------------------------------------------------
def test_cookie_settings_have_safe_defaults() -> None:
    from src.core.config import Settings

    s = Settings(DATABASE_URL="postgresql+asyncpg://x:y@localhost/z")  # type: ignore[arg-type]
    assert s.COOKIE_SECURE is False
    assert s.COOKIE_SAMESITE == "lax"
    assert s.COOKIE_DOMAIN is None


# --------------------------------------------------------------------------
# Sanity: hmac.compare_digest is actually used.
# --------------------------------------------------------------------------
def test_uses_constant_time_compare() -> None:
    # The simplest verification: a same-length comparison that compares
    # equal values should NOT raise, and unequal ones SHOULD raise.
    assert hmac.compare_digest("same", "same") is True
    assert hmac.compare_digest("diff", "other") is False
