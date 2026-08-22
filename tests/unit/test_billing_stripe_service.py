"""Unit tests for billing.stripe_service.

Focus: the SDK wrapper's edge cases that don't require network:
  - `verify_webhook`: signature pass/fail, missing header, missing secret
  - `create_checkout_session`: rejects when Stripe isn't configured
  - `create_checkout_session`: rejects when the plan has no price id
  - `_plan_from_price_id` mapping (covered in webhook tests too)

Real Stripe SDK calls are tested in the integration suite where they
hit the Stripe test mode (`sk_test_…`).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.core.config import Settings
from src.core.exceptions import ValidationError
from src.modules.billing.stripe_service import (
    BillingError,
    BillingService,
    StripeSignatureInvalidError,
    build_billing_service,
)
from src.shared.domain.enums import AgencySubscriptionPlan


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture()
def settings_with_billing() -> Settings:
    """A Settings instance with Stripe keys populated.

    We construct a Settings with overrides to avoid clobbering env vars.
    """
    return Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://u:p@h/db",
        STRIPE_SECRET_KEY="sk_test_fake",
        STRIPE_WEBHOOK_SECRET="whsec_fake",
        STRIPE_PRICE_BASIC="price_basic_test",
        STRIPE_PRICE_PROFESSIONAL="price_pro_test",
        STRIPE_PRICE_ENTERPRISE="price_ent_test",
        STRIPE_CHECKOUT_SUCCESS_URL="http://localhost:3000/success",
        STRIPE_CHECKOUT_CANCEL_URL="http://localhost:3000/cancel",
    )


@pytest.fixture()
def settings_no_billing() -> Settings:
    return Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://u:p@h/db",
        # All Stripe keys None — billing disabled.
    )


# --------------------------------------------------------------------------
# verify_webhook
# --------------------------------------------------------------------------
def test_verify_webhook_returns_event_when_signature_valid(settings_with_billing: Settings) -> None:
    service = BillingService(settings_with_billing)
    # Replace the SDK with a stub that returns a known event dict.
    service._stripe = MagicMock()
    service._stripe.Webhook.construct_event.return_value = SimpleNamespace(
        to_dict=lambda: {"id": "evt_1", "type": "ping"},
    )

    event = service.verify_webhook(payload=b"{}", signature_header="t=1,v1=abc")
    assert event["id"] == "evt_1"


def test_verify_webhook_raises_on_missing_signature_header(settings_with_billing: Settings) -> None:
    service = BillingService(settings_with_billing)
    with pytest.raises(StripeSignatureInvalidError):
        service.verify_webhook(payload=b"{}", signature_header=None)


def test_verify_webhook_raises_on_bad_signature(settings_with_billing: Settings) -> None:
    service = BillingService(settings_with_billing)

    # Build a real exception subclass matching the SDK's name so our
    # `except self._stripe.error.SignatureVerificationError` clause
    # actually matches.
    class _FakeSignatureError(Exception):
        pass

    service._stripe = MagicMock()
    service._stripe.error.SignatureVerificationError = _FakeSignatureError
    service._stripe.Webhook.construct_event.side_effect = _FakeSignatureError("bad sig")

    with pytest.raises(StripeSignatureInvalidError):
        service.verify_webhook(payload=b"{}", signature_header="t=1,v1=bad")


def test_verify_webhook_raises_billing_error_when_secret_unset(settings_no_billing: Settings) -> None:
    # `STRIPE_WEBHOOK_SECRET=None` means we can't verify anything.
    service = BillingService(settings_no_billing)
    with pytest.raises(BillingError):
        service.verify_webhook(payload=b"{}", signature_header="t=1,v1=abc")


# --------------------------------------------------------------------------
# create_checkout_session
# --------------------------------------------------------------------------
def test_create_checkout_session_returns_url_and_id(settings_with_billing: Settings) -> None:
    service = BillingService(settings_with_billing)
    service._stripe = MagicMock()
    service._stripe.checkout.Session.create.return_value = SimpleNamespace(
        url="https://stripe.com/session/cs_test",
        id="cs_test_abc",
    )

    result = service.create_checkout_session(
        agency_id=str(uuid.uuid4()),
        customer_id="cus_test_1",
        plan=AgencySubscriptionPlan.PROFESSIONAL,
        success_url="http://localhost/success",
        cancel_url="http://localhost/cancel",
    )
    assert result.url == "https://stripe.com/session/cs_test"
    assert result.session_id == "cs_test_abc"

    # Verify the SDK was called with the configured price id.
    call = service._stripe.checkout.Session.create.call_args
    assert call.kwargs["mode"] == "subscription"
    assert call.kwargs["customer"] == "cus_test_1"
    assert call.kwargs["line_items"] == [{"price": "price_pro_test", "quantity": 1}]


def test_create_checkout_session_rejects_unconfigured_plan() -> None:
    """If STRIPE_PRICE_* is missing for the chosen plan, raise 422."""
    s = Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://u:p@h/db",
        STRIPE_SECRET_KEY="sk_test_fake",
        STRIPE_PRICE_BASIC="price_basic_test",
        # PROFESSIONAL and ENTERPRISE intentionally None
    )
    service = BillingService(s)
    with pytest.raises(ValidationError) as exc_info:
        service.create_checkout_session(
            agency_id="ag_1",
            customer_id="cus_test_1",
            plan=AgencySubscriptionPlan.ENTERPRISE,
            success_url="http://localhost/success",
            cancel_url="http://localhost/cancel",
        )
    assert "ENTERPRISE" in str(exc_info.value)


def test_create_checkout_session_raises_billing_error_when_not_configured(
    settings_no_billing: Settings,
) -> None:
    service = BillingService(settings_no_billing)
    with pytest.raises(BillingError):
        service.create_checkout_session(
            agency_id="ag_1",
            customer_id="cus_test_1",
            plan=AgencySubscriptionPlan.BASIC,
            success_url="http://localhost/success",
            cancel_url="http://localhost/cancel",
        )


# --------------------------------------------------------------------------
# stripe_configured property
# --------------------------------------------------------------------------
def test_stripe_configured_true_when_keys_present(settings_with_billing: Settings) -> None:
    assert settings_with_billing.stripe_configured is True


def test_stripe_configured_false_when_secret_missing() -> None:
    s = Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://u:p@h/db",
        STRIPE_PRICE_BASIC="price_basic_test",
    )
    assert s.stripe_configured is False


def test_stripe_configured_false_when_no_prices_set() -> None:
    s = Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://u:p@h/db",
        STRIPE_SECRET_KEY="sk_test_fake",
    )
    assert s.stripe_configured is False


# --------------------------------------------------------------------------
# stripe_price_id_for property
# --------------------------------------------------------------------------
def test_stripe_price_id_for_maps_all_three_plans(settings_with_billing: Settings) -> None:
    mapping = settings_with_billing.stripe_price_id_for
    assert mapping["BASIC"] == "price_basic_test"
    assert mapping["PROFESSIONAL"] == "price_pro_test"
    assert mapping["ENTERPRISE"] == "price_ent_test"


# --------------------------------------------------------------------------
# build_billing_service factory
# --------------------------------------------------------------------------
def test_build_billing_service_returns_instance(settings_with_billing: Settings) -> None:
    svc = build_billing_service(settings_with_billing)
    assert isinstance(svc, BillingService)
    assert svc._settings is settings_with_billing
