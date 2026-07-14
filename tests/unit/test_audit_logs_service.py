"""Unit tests for audit_logs service helpers — `request_ip_ua`.

Pure-Python tests — no DB. We construct a stub `Request` with the same
attributes the helper reads (headers, client).
"""

from __future__ import annotations

from types import SimpleNamespace

from src.modules.audit_logs.service import request_ip_ua


class _StubClient:
    def __init__(self, host: str | None):
        self.host = host


class _StubRequest:
    def __init__(
        self,
        *,
        x_forwarded_for: str | None = None,
        host: str | None = None,
        user_agent: str | None = None,
    ):
        headers: dict[str, str] = {}
        if x_forwarded_for is not None:
            headers["x-forwarded-for"] = x_forwarded_for
        if user_agent is not None:
            headers["user-agent"] = user_agent
        self.headers = headers
        self.client = _StubClient(host)


def test_request_ip_ua_prefers_xff() -> None:
    req = _StubRequest(
        x_forwarded_for="203.0.113.1, 10.0.0.1, 10.0.0.2",
        host="127.0.0.1",
        user_agent="curl/8.0",
    )
    ip, ua = request_ip_ua(req)
    assert ip == "203.0.113.1"
    assert ua == "curl/8.0"


def test_request_ip_ua_falls_back_to_client_host() -> None:
    req = _StubRequest(host="198.51.100.7", user_agent="Mozilla/5.0")
    ip, ua = request_ip_ua(req)
    assert ip == "198.51.100.7"
    assert ua == "Mozilla/5.0"


def test_request_ip_ua_handles_missing_headers() -> None:
    req = _StubRequest(host="127.0.0.1")
    ip, ua = request_ip_ua(req)
    assert ip == "127.0.0.1"
    assert ua is None


def test_request_ip_ua_handles_missing_client() -> None:
    req = SimpleNamespace(headers={"user-agent": "x"}, client=None)
    ip, ua = request_ip_ua(req)
    assert ip is None
    assert ua == "x"


def test_request_ip_ua_returns_none_pair_for_none_request() -> None:
    ip, ua = request_ip_ua(None)
    assert ip is None
    assert ua is None


def test_request_ip_ua_trims_xff_whitespace() -> None:
    req = _StubRequest(
        x_forwarded_for="   203.0.113.5   , 10.0.0.1",
        host="127.0.0.1",
    )
    ip, ua = request_ip_ua(req)
    assert ip == "203.0.113.5"
    assert ua is None
