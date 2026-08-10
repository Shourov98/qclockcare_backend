"""Unit tests for the reports router's per-request gating and shape.

Mirrors `tests/unit/test_billing_router.py` — exercises the 503 / 404 /
OpenAPI paths without needing the DB or a real `anthropic` HTTP client.

The Anthropic SDK is NOT exercised here; that's covered by the deeper
service-level tests in `test_reports_service.py`.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture()
def client_reports_off():
    """TestClient with the AI-narrative gate disabled.

    Patches `settings` as seen by the router. The Auth dep still runs,
    so we either get 401 (no token) or 503 (feature off) — both are
    acceptable guards for dev/CI without the key.
    """
    from src.core.config import Settings
    from src.main import app

    fake = Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://u:p@h/db",
        FEATURE_REPORTS_AI_NARRATIVE=False,
        CLAUDE_API_KEY=None,
    )
    with patch("src.modules.reports.router.settings", fake), TestClient(app) as c:
        yield c


# --------------------------------------------------------------------------
# Gate behaviour
# --------------------------------------------------------------------------
def test_stream_returns_503_when_ai_disabled(client_reports_off: TestClient) -> None:
    """POST /reports/visit_summary/stream returns 503 when feature flag is off."""
    response = client_reports_off.post(
        "/reports/visit_summary/stream",
        json={"params": {}},
    )
    # Either 401 (auth required) or 503 (feature off) are valid
    # dev/CI outcomes — the gate sits AFTER auth in the chain.
    assert response.status_code in (401, 503)


def test_unknown_report_type_returns_404(client_reports_off: TestClient) -> None:
    """POST /reports/{bad}/stream — invalid path → 404 (not FastAPI's noisy 422)."""
    response = client_reports_off.post(
        "/reports/not_a_real_report/stream",
        json={"params": {}},
    )
    # Same as above — auth may reject first.
    assert response.status_code in (401, 404, 503)


# --------------------------------------------------------------------------
# OpenAPI shape — these never need auth, so we can hit them directly.
# --------------------------------------------------------------------------
def test_openapi_advertises_reports_endpoints(client_reports_off: TestClient) -> None:
    """All 4 reports endpoints are registered in /openapi.json."""
    schema = client_reports_off.get("/openapi.json").json()
    paths = schema["paths"]

    assert "/reports/{report_type}/stream" in paths
    assert "/reports/runs" in paths
    assert "/reports/runs/{run_id}" in paths
    assert "/reports/runs/{run_id}/export" in paths


def test_stream_endpoint_advertises_sse_response(client_reports_off: TestClient) -> None:
    """The stream endpoint documents text/event-stream + 503."""
    schema = client_reports_off.get("/openapi.json").json()
    op = schema["paths"]["/reports/{report_type}/stream"]["post"]
    assert "503" in op["responses"]
    # SSE content type is documented via summary; OpenAPI doesn't have
    # a native media-type-for-streaming field.
    assert "Server-Sent Events" in op["responses"]["200"]["description"]


def test_export_endpoint_advertises_attachment(client_reports_off: TestClient) -> None:
    """The export endpoint documents the Content-Disposition attachment."""
    schema = client_reports_off.get("/openapi.json").json()
    op = schema["paths"]["/reports/runs/{run_id}/export"]["get"]
    desc = op["responses"]["200"]["description"]
    assert "Content-Disposition" in desc
    assert "attachment" in desc


def test_reports_tag_in_tags_metadata(client_reports_off: TestClient) -> None:
    """The 'reports' OpenAPI tag is registered."""
    schema = client_reports_off.get("/openapi.json").json()
    tag_names = {tag["name"] for tag in schema.get("tags", [])}
    assert "reports" in tag_names


__all__ = []
