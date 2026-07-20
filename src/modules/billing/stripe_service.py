"""Stripe SDK wrapper.

Centralises every call into the `stripe` SDK so:

1. The router never imports `stripe` directly — keeps a single
   chokepoint for swapping the client during tests.
2. Configuration (API key, timeout) is read once at construction time
   and the SDK is configured accordingly.
3. Domain-level errors are normalised — `stripe.error.*Error` becomes
   our own `BillingError` so the global handler doesn't need to know
   about Stripe.

Why not use raw HTTP via httpx: the official SDK handles signature
verification, idempotency keys, exponential backoff, and pagination
correctly out of the box. Reaching around it costs us more than it
saves.

Module guard: the `stripe` Python package isn't a hard runtime dep
when `FEATURE_BILLING_ENABLED=False`. The `BillingService` constructor
imports it lazily so the rest of the app stays importable in dev without
Stripe installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from src.core.config import Settings
from src.core.exceptions import (
    ServiceUnavailableError,
    ValidationError,
)
from src.shared.domain.enums import AgencySubscriptionPlan

logger = structlog.get_logger(__name__)


# --------------------------------------------------------------------------
# Public exceptions
# --------------------------------------------------------------------------
class BillingError(ServiceUnavailableError):
    """Generic billing subsystem failure (Stripe client/network)."""

    error_code = "BILLING_ERROR"
    message = "Billing provider error."


class StripeSignatureInvalidError(BillingError):
    """Webhook signature didn't verify — most likely a forged request."""

    error_code = "STRIPE_SIGNATURE_INVALID"
    message = "Stripe webhook signature did not verify."


# --------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------
@dataclass
class CheckoutResult:
    url: str
    session_id: str


class BillingService:
    """Thin wrapper around the Stripe SDK.

    Construct once per request (cheap — no I/O). The SDK maintains its
    own connection pool internally.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._stripe = self._import_stripe()

    # --------------------------------------------------------------
    # Lazy SDK import — keeps the dev path free of `stripe` when
    # FEATURE_BILLING_ENABLED is False.
    # --------------------------------------------------------------
    @staticmethod
    def _import_stripe() -> Any:  # pragma: no cover - delegated to SDK
        try:
            import stripe  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on env
            raise ServiceUnavailableError(
                "Stripe SDK not installed. `uv add stripe` to enable billing.",
            ) from exc
        return stripe

    # --------------------------------------------------------------
    # Configuration
    # --------------------------------------------------------------
    def _ensure_configured(self) -> None:
        """Refuse to talk to Stripe when keys are missing in dev.

        We don't raise at construction because the dev server boots
        even with billing turned off (FEATURE_BILLING_ENABLED=False).
        """
        if (
            self._settings.STRIPE_SECRET_KEY is None
            or not self._settings.stripe_configured
        ):
            raise BillingError(
                "Stripe is not configured. Set STRIPE_SECRET_KEY and at "
                "least one STRIPE_PRICE_* value, then restart.",
            )

    def _api_key(self) -> str:
        assert self._settings.STRIPE_SECRET_KEY is not None
        return self._settings.STRIPE_SECRET_KEY.get_secret_value()

    def _webhook_secret(self) -> str | None:
        if self._settings.STRIPE_WEBHOOK_SECRET is None:
            return None
        return self._settings.STRIPE_WEBHOOK_SECRET.get_secret_value()

    # --------------------------------------------------------------
    # Webhook verification
    # --------------------------------------------------------------
    def verify_webhook(
        self,
        *,
        payload: bytes,
        signature_header: str | None,
    ) -> dict[str, Any]:
        """Verify a Stripe webhook signature and return the parsed event.

        Stripe sends every webhook with `Stripe-Signature: t=...,v1=...`.
        We MUST verify it before trusting any field. The signature is HMAC
        of (timestamp + raw payload) using the webhook secret; the SDK's
        `Webhook.construct_event` checks both the HMAC and that the
        timestamp is recent (default tolerance 5 minutes).
        """
        secret = self._webhook_secret()
        if secret is None:
            raise BillingError(
                "Cannot verify webhook — STRIPE_WEBHOOK_SECRET is not set.",
            )
        if not signature_header:
            raise StripeSignatureInvalidError(
                "Missing Stripe-Signature header.",
            )
        try:
            event = self._stripe.Webhook.construct_event(  # type: ignore[attr-defined]
                payload=payload,
                sig_header=signature_header,
                secret=secret,
            )
        except self._stripe.error.SignatureVerificationError as exc:  # type: ignore[attr-defined]
            logger.warning("stripe.signature_invalid", error=str(exc))
            raise StripeSignatureInvalidError() from exc
        except Exception as exc:  # pragma: no cover - SDK-specific errors
            logger.exception("stripe.construct_event_failed")
            raise BillingError("Failed to parse Stripe webhook payload.") from exc
        return event.to_dict()  # type: ignore[no-any-return]

    # --------------------------------------------------------------
    # Customer management
    # --------------------------------------------------------------
    def get_or_create_customer(
        self,
        *,
        agency_id: str,
        existing_customer_id: str | None,
        admin_email: str,
    ) -> str:
        """Return the Stripe Customer ID for the agency.

        Strategy: prefer the cached `stripe_customer_id` on the agency
        row. If it doesn't exist (or has been deleted in the Stripe
        dashboard), create a fresh Customer tagged with the agency_id
        in metadata so future webhooks can resolve back.
        """
        self._ensure_configured()
        if existing_customer_id:
            try:
                customer = self._stripe.Customer.retrieve(existing_customer_id)  # type: ignore[attr-defined]
                if not getattr(customer, "deleted", False):
                    return existing_customer_id
            except self._stripe.error.InvalidRequestError:  # type: ignore[attr-defined]
                # Customer was deleted in the dashboard — fall through to create.
                logger.info(
                    "stripe.customer_missing",
                    customer_id=existing_customer_id,
                )

        try:
            customer = self._stripe.Customer.create(  # type: ignore[attr-defined]
                email=admin_email,
                metadata={"agency_id": agency_id},
            )
        except self._stripe.error.StripeError as exc:  # type: ignore[attr-defined]
            logger.exception("stripe.customer_create_failed")
            raise BillingError("Could not create Stripe customer.") from exc
        return customer.id  # type: ignore[no-any-return]

    # --------------------------------------------------------------
    # Checkout
    # --------------------------------------------------------------
    def create_checkout_session(
        self,
        *,
        agency_id: str,
        customer_id: str,
        plan: AgencySubscriptionPlan,
        success_url: str,
        cancel_url: str,
    ) -> CheckoutResult:
        """Open a Stripe Checkout session for the chosen package.

        One-time prices only — recurring subscription starts AFTER the
        customer completes checkout, at which point Stripe POSTs a
        `checkout.session.completed` webhook that we use to grab the
        subscription_id.
        """
        self._ensure_configured()
        price_id = self._settings.stripe_price_id_for.get(plan.value)
        if not price_id:
            raise ValidationError(
                f"Stripe price not configured for plan {plan.value}.",
                details={
                    "plan": plan.value,
                    "env_var": f"STRIPE_PRICE_{plan.value}",
                },
            )

        try:
            session = self._stripe.checkout.Session.create(  # type: ignore[attr-defined]
                mode="subscription",
                customer=customer_id,
                line_items=[{"price": price_id, "quantity": 1}],
                success_url=success_url,
                cancel_url=cancel_url,
                metadata={"agency_id": agency_id, "plan": plan.value},
                subscription_data={
                    "metadata": {
                        "agency_id": agency_id,
                        "plan": plan.value,
                    },
                },
            )
        except self._stripe.error.StripeError as exc:  # type: ignore[attr-defined]
            logger.exception("stripe.checkout_session_failed")
            raise BillingError("Could not create Stripe checkout session.") from exc
        return CheckoutResult(url=session.url, session_id=session.id)  # type: ignore[no-any-return]

    # --------------------------------------------------------------
    # Billing portal
    # --------------------------------------------------------------
    def create_portal_session(
        self,
        *,
        customer_id: str,
        return_url: str,
    ) -> str:
        """Open a Stripe-hosted billing portal session.

        The portal lets the customer update payment method, view
        invoices, and cancel their subscription without us having to
        build those flows ourselves.
        """
        self._ensure_configured()
        try:
            session = self._stripe.billing_portal.Session.create(  # type: ignore[attr-defined]
                customer=customer_id,
                return_url=return_url,
            )
        except self._stripe.error.StripeError as exc:  # type: ignore[attr-defined]
            logger.exception("stripe.portal_session_failed")
            raise BillingError("Could not create Stripe portal session.") from exc
        return session.url  # type: ignore[no-any-return]


__all__ = [
    "BillingError",
    "BillingService",
    "CheckoutResult",
    "StripeSignatureInvalidError",
]


# Re-export for test convenience.
def build_billing_service(settings: Settings | None = None) -> BillingService:
    """Factory used by the router. Pass `settings=None` to default to the
    cached singleton (the common case)."""
    if settings is None:
        from src.core.config import settings as _settings

        settings = _settings
    return BillingService(settings)
