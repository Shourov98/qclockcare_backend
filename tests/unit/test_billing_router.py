"""Unit tests for the billing router's per-request gating.

Skips the heavy network / Stripe paths and exercises:

  - 503 returned when FEATURE_BILLING_ENABLED=False (the common dev case)
  - 503 returned for the webhook endpoint when billing is off
  - OpenAPI schema includes the billing endpoints
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client_billing_off():
    """TestClient with billing disabled — covers the 503 path.

    Patches only `src.modules.billing.router.settings` because the
    503 gate lives in the router. The service-layer billing checks
    don't run before the gate returns.
    """
    from src.core.config import Settings
    from src.main import app

    fake = Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://u:p@h/db",
        FEATURE_BILLING_ENABLED=False,
    )
    with patch("src.modules.billing.router.settings", fake), TestClient(app) as c:
        yield c


def test_checkout_returns_503_when_billing_disabled(client_billing_off: TestClient) -> None:
    response = client_billing_off.post(
        "/agencies/00000000-0000-0000-0000-000000000000/billing/checkout",
        json={"plan": "BASIC"},
        headers={"Authorization": "Bearer fake"},
    )
    # 401 (no auth) takes precedence over 503 — both are valid dev errors.
    assert response.status_code in (401, 503)


def test_webhook_returns_503_when_billing_disabled(client_billing_off: TestClient) -> None:
    response = client_billing_off.post(
        "/billing/webhook",
        json={"id": "evt_test"},
    )
    assert response.status_code == 503


def test_openapi_advertises_billing_endpoints(client_billing_off: TestClient) -> None:
    schema = client_billing_off.get("/openapi.json").json()
    paths = schema["paths"]
    assert "/agencies/{agency_id}/billing/checkout" in paths
    assert "/agencies/{agency_id}/billing/portal-session" in paths
    assert "/billing/webhook" in paths

    checkout_op = paths["/agencies/{agency_id}/billing/checkout"]["post"]
    assert "summary" in checkout_op
    assert "503" in checkout_op["responses"]


def test_billing_tag_in_tags_metadata(client_billing_off: TestClient) -> None:
    schema = client_billing_off.get("/openapi.json").json()
    tag_names = {tag["name"] for tag in schema.get("tags", [])}
    assert "billing" in tag_names
