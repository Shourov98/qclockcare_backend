"""Unit tests for billing webhook handler.

Covers:
  - Six supported event types each mutate the agency row correctly.
  - Duplicate delivery short-circuits at the PK constraint.
  - Orphan events (no matching agency) still return a 200 outcome.
  - Unknown event types are recorded but not applied.
  - `invoice.payment_failed` flips ACTIVE → SUSPENDED and stamps
    `payment_failed_at`.
  - `invoice.payment_succeeded` recovers SUSPENDED → ACTIVE.
  - `customer.subscription.deleted` clears Stripe IDs and sets CHURNED.

The test exercises the sync helpers (`_apply_subscription_deleted`,
`_apply_payment_failed`, `_apply_payment_succeeded`,
`_apply_subscription_created_or_updated`, `_apply_checkout_completed`)
plus the price-id → plan mapping. The async database orchestration
(`handle_stripe_event`) is exercised by the integration tests so they
run alongside the real local Supabase stack — those are skipped here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from src.modules.agencies.models import Agency

# Import related model modules so all relationship() references resolve
# before the test instantiates Agency. SQLAlchemy resolves strings
# lazily but the resolution has to happen before the first mapper is
# configured against the registry.
from src.modules.appointments import models as _appt_models  # noqa: F401
from src.modules.billing.webhook_service import (
    _apply_checkout_completed,
    _apply_payment_failed,
    _apply_payment_succeeded,
    _apply_subscription_created_or_updated,
    _apply_subscription_deleted,
    _plan_from_price_id,
)
from src.modules.identity import models as _identity_models  # noqa: F401
from src.modules.patients import models as _patient_models  # noqa: F401
from src.modules.staff import models as _staff_models  # noqa: F401
from src.modules.visits import models as _visits_models  # noqa: F401
from src.shared.domain.enums import AgencyStatus, AgencySubscriptionPlan


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture()
def agency() -> Agency:
    """A bare Agency with TRIAL status — every test mutates from this."""
    return Agency(
        id=uuid.uuid4(),
        name="Test Agency",
        timezone="America/Chicago",
        status=AgencyStatus.TRIAL,
        subscription_plan=AgencySubscriptionPlan.PROFESSIONAL,
        subscription_price_cents=7900,
        subscription_billing_cycle="MONTHLY",
        trial_started_at=datetime.now(UTC),
        trial_ends_at=datetime.now(UTC),
        settings={},
    )


@pytest.fixture()
def price_map() -> dict[str, str | None]:
    return {
        AgencySubscriptionPlan.BASIC.value: "price_basic_test",
        AgencySubscriptionPlan.PROFESSIONAL.value: "price_pro_test",
        AgencySubscriptionPlan.ENTERPRISE.value: "price_ent_test",
    }


# --------------------------------------------------------------------------
# Plan mapping
# --------------------------------------------------------------------------
def test_plan_from_price_id_resolves_known_prices(price_map: dict) -> None:
    assert _plan_from_price_id(price_map, "price_basic_test") == AgencySubscriptionPlan.BASIC
    assert _plan_from_price_id(price_map, "price_pro_test") == AgencySubscriptionPlan.PROFESSIONAL
    assert _plan_from_price_id(price_map, "price_ent_test") == AgencySubscriptionPlan.ENTERPRISE


def test_plan_from_price_id_returns_none_for_unknown(price_map: dict) -> None:
    assert _plan_from_price_id(price_map, "price_unknown") is None
    assert _plan_from_price_id(price_map, None) is None


# --------------------------------------------------------------------------
# checkout.session.completed
# --------------------------------------------------------------------------
def test_checkout_completed_stamps_subscription_id_and_activates(agency: Agency, price_map: dict) -> None:
    data = {
        "subscription": "sub_test_1",
        "metadata": {"plan": "PROFESSIONAL"},
    }
    _apply_checkout_completed(agency, data, price_map)
    assert agency.stripe_subscription_id == "sub_test_1"
    # TRIAL → ACTIVE on first payment.
    assert agency.status == AgencyStatus.ACTIVE


def test_checkout_completed_no_op_for_missing_agency(price_map: dict) -> None:
    _apply_checkout_completed(None, {"subscription": "sub_x"}, price_map)
    # No exception, no state mutation possible (agency is None).


def test_checkout_completed_keeps_subscription_id_if_already_set(
    agency: Agency, price_map: dict
) -> None:
    agency.stripe_subscription_id = "sub_existing"
    _apply_checkout_completed(
        agency,
        {"subscription": "sub_new"},
        price_map,
    )
    assert agency.stripe_subscription_id == "sub_existing"


# --------------------------------------------------------------------------
# customer.subscription.created/updated
# --------------------------------------------------------------------------
def test_subscription_updated_active_sets_period_and_price(
    agency: Agency, price_map: dict
) -> None:
    now_epoch = int(datetime.now(UTC).timestamp())
    data = {
        "status": "active",
        "current_period_start": now_epoch,
        "current_period_end": now_epoch + 30 * 86400,
        "cancel_at_period_end": False,
        "items": {"data": [{"price": {"id": "price_pro_test"}}]},
    }
    _apply_subscription_created_or_updated(agency, data, price_map, is_created=False)
    assert agency.status == AgencyStatus.ACTIVE
    assert agency.stripe_price_id == "price_pro_test"
    assert agency.subscription_plan == AgencySubscriptionPlan.PROFESSIONAL
    assert agency.current_period_end is not None


def test_subscription_updated_trialing_sets_trial(agency: Agency, price_map: dict) -> None:
    agency.status = AgencyStatus.SUSPENDED  # previously suspended
    _apply_subscription_created_or_updated(
        agency,
        {"status": "trialing", "items": {"data": []}},
        price_map,
        is_created=True,
    )
    assert agency.status == AgencyStatus.TRIAL


def test_subscription_updated_past_due_keeps_active(agency: Agency, price_map: dict) -> None:
    """past_due is recoverable — agency stays usable while dunning runs."""
    agency.status = AgencyStatus.ACTIVE
    _apply_subscription_created_or_updated(
        agency,
        {"status": "past_due", "items": {"data": []}},
        price_map,
        is_created=False,
    )
    assert agency.status == AgencyStatus.ACTIVE


def test_subscription_updated_unpaid_suspends(agency: Agency, price_map: dict) -> None:
    agency.status = AgencyStatus.ACTIVE
    _apply_subscription_created_or_updated(
        agency,
        {"status": "unpaid", "items": {"data": []}},
        price_map,
        is_created=False,
    )
    assert agency.status == AgencyStatus.SUSPENDED


def test_subscription_updated_cancel_at_period_end_flag(agency: Agency, price_map: dict) -> None:
    _apply_subscription_created_or_updated(
        agency,
        {"status": "active", "cancel_at_period_end": True, "items": {"data": []}},
        price_map,
        is_created=False,
    )
    assert agency.cancel_at_period_end is True


def test_subscription_updated_unknown_price_does_not_change_plan(
    agency: Agency, price_map: dict
) -> None:
    agency.subscription_plan = AgencySubscriptionPlan.ENTERPRISE
    _apply_subscription_created_or_updated(
        agency,
        {
            "status": "active",
            "items": {"data": [{"price": {"id": "price_unknown"}}]},
        },
        price_map,
        is_created=False,
    )
    # Plan unchanged because we couldn't resolve the price.
    assert agency.subscription_plan == AgencySubscriptionPlan.ENTERPRISE


# --------------------------------------------------------------------------
# customer.subscription.deleted
# --------------------------------------------------------------------------
def test_subscription_deleted_churns_and_clears_ids(agency: Agency) -> None:
    agency.stripe_subscription_id = "sub_old"
    agency.stripe_price_id = "price_old"
    agency.current_period_end = datetime.now(UTC)
    agency.cancel_at_period_end = True

    _apply_subscription_deleted(agency)

    assert agency.status == AgencyStatus.CHURNED
    assert agency.stripe_subscription_id is None
    assert agency.stripe_price_id is None
    assert agency.current_period_end is None
    assert agency.cancel_at_period_end is False


def test_subscription_deleted_no_op_for_missing_agency() -> None:
    _apply_subscription_deleted(None)  # must not raise


# --------------------------------------------------------------------------
# invoice.payment_failed
# --------------------------------------------------------------------------
def test_payment_failed_suspends_and_stamps_settings(agency: Agency) -> None:
    agency.status = AgencyStatus.ACTIVE
    agency.settings = {}

    _apply_payment_failed(agency)

    assert agency.status == AgencyStatus.SUSPENDED
    assert "payment_failed_at" in agency.settings


def test_payment_failed_preserves_existing_settings(agency: Agency) -> None:
    agency.status = AgencyStatus.ACTIVE
    agency.settings = {"branding_color": "blue"}

    _apply_payment_failed(agency)

    assert agency.settings.get("branding_color") == "blue"
    assert "payment_failed_at" in agency.settings


def test_payment_failed_no_op_for_missing_agency() -> None:
    _apply_payment_failed(None)  # must not raise


# --------------------------------------------------------------------------
# invoice.payment_succeeded
# --------------------------------------------------------------------------
def test_payment_succeeded_recovers_from_suspension(agency: Agency) -> None:
    agency.status = AgencyStatus.SUSPENDED
    agency.settings = {"payment_failed_at": "2026-07-01T00:00:00Z"}

    _apply_payment_succeeded(agency)

    assert agency.status == AgencyStatus.ACTIVE
    assert "payment_failed_at" not in agency.settings


def test_payment_succeeded_does_not_downgrade_active(agency: Agency) -> None:
    agency.status = AgencyStatus.ACTIVE
    agency.settings = {"other_setting": "value"}

    _apply_payment_succeeded(agency)

    # ACTIVE stays ACTIVE — handler is purely a recovery path.
    assert agency.status == AgencyStatus.ACTIVE
    assert agency.settings == {"other_setting": "value"}


def test_payment_succeeded_no_op_for_missing_agency() -> None:
    _apply_payment_succeeded(None)  # must not raise
