"""Unit tests for agency subscription package definitions."""

from __future__ import annotations

from src.modules.agencies import service as agencies_service
from src.shared.domain.enums import AgencySubscriptionPlan


def test_subscription_packages_match_configured_pricing_cards() -> None:
    packages = agencies_service.list_subscription_packages()

    assert [p["plan"] for p in packages] == [
        AgencySubscriptionPlan.BASIC,
        AgencySubscriptionPlan.PROFESSIONAL,
        AgencySubscriptionPlan.ENTERPRISE,
    ]
    assert [p["monthly_price_cents"] for p in packages] == [2900, 7900, 9000]
    assert all(p["billing_cycle"] == "MONTHLY" for p in packages)


def test_professional_package_is_marked_most_popular() -> None:
    packages = agencies_service.list_subscription_packages()
    most_popular = [p for p in packages if p["is_most_popular"]]

    assert len(most_popular) == 1
    assert most_popular[0]["plan"] == AgencySubscriptionPlan.PROFESSIONAL
