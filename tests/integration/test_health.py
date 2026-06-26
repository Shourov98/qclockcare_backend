"""Smoke tests for /health and /ready.

These verify the skeleton wiring (router, exception handlers, middleware
ordering, settings) without requiring a real Postgres connection.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_returns_ok(client: TestClient) -> None:
    """`/health` must always 200 with metadata about the app."""
    resp = client.get("/health")
    assert resp.status_code == 200

    body = resp.json()
    assert body["status"] == "ok"
    # `env` reflects APP_ENV — may be "development" if .env was loaded, or
    # "test" if no .env. Both are valid; we just verify the field is present.
    assert body["env"] in ("test", "development")
    assert "app" in body
    assert "version" in body


def test_request_id_round_trip(client: TestClient) -> None:
    """An inbound X-Request-ID must come back on the response."""
    resp = client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert resp.headers.get("X-Request-ID") == "abc-123"


def test_request_id_generated_when_missing(client: TestClient) -> None:
    """If the client doesn't send X-Request-ID, we generate one."""
    resp = client.get("/health")
    rid = resp.headers.get("X-Request-ID")
    assert rid is not None
    assert len(rid) > 0


def test_404_returns_standard_envelope(client: TestClient) -> None:
    """Unknown routes → standard error envelope with the right shape."""
    resp = client.get("/this-route-does-not-exist")
    assert resp.status_code == 404
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == "NOT_FOUND"
    assert "request_id" in body["error"]
    assert "timestamp" in body["error"]
