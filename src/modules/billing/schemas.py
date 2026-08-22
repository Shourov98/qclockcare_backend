"""Billing module — request/response DTOs.

These shapes are intentionally *narrow* — they only describe what we
return to clients of the `/billing/*` endpoints. The Stripe SDK's own
event types are kept *inside* the webhook handler so we never expose
Stripe's payload schema on our public API.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.shared.domain.enums import AgencySubscriptionPlan


# --------------------------------------------------------------------------
# Checkout
# --------------------------------------------------------------------------
class CheckoutRequest(BaseModel):
    """Body for `POST /agencies/{agency_id}/billing/checkout`.

    The customer is always the AGENCY_ADMIN (or SUPER_ADMIN) making the
    request — we look them up via the bearer token. No email/name is
    accepted from the client: Stripe creates/updates the Customer from
    whatever we already have on file for the agency admin.
    """

    plan: AgencySubscriptionPlan = Field(
        description="The package the agency wants to subscribe to.",
    )
    success_url: str | None = Field(
        default=None,
        description="Override STRIPE_CHECKOUT_SUCCESS_URL for this session only.",
    )
    cancel_url: str | None = Field(
        default=None,
        description="Override STRIPE_CHECKOUT_CANCEL_URL for this session only.",
    )


class CheckoutResponse(BaseModel):
    """Body returned by `POST /agencies/{agency_id}/billing/checkout`.

    Frontend redirects the browser to `checkout_url`; Stripe completes
    the rest of the flow and POSTs back to `/billing/webhook`.
    """

    checkout_url: str
    session_id: str


# --------------------------------------------------------------------------
# Billing portal
# --------------------------------------------------------------------------
class PortalSessionResponse(BaseModel):
    """Body returned by `POST /agencies/{agency_id}/billing/portal-session`.

    `portal_url` is a one-shot Stripe-hosted URL the agency admin uses
    to update payment method, view invoices, or cancel.
    """

    portal_url: str


# --------------------------------------------------------------------------
# Webhook ack
# --------------------------------------------------------------------------
class WebhookAck(BaseModel):
    """Tiny ack envelope returned by the webhook endpoint.

    Stripe only cares that we return 2xx; this body exists for our
    own request logs and for the few custom clients that might POST
    here in dev.
    """

    received: Literal[True] = True
    event_id: str
    event_type: str
    duplicate: bool = False


# --------------------------------------------------------------------------
# Webhook event audit (SUPER_ADMIN debug)
# --------------------------------------------------------------------------
class WebhookEventRecord(BaseModel):
    """One processed Stripe webhook event (for `/billing/events` debug)."""

    model_config = ConfigDict(from_attributes=True)

    stripe_event_id: str
    event_type: str
    agency_id: UUID | None
    processed_at: str  # ISO-8601 — JSONB-friendly
    payload: dict


class WebhookEventListResponse(BaseModel):
    data: list[WebhookEventRecord]


__all__ = [
    "CheckoutRequest",
    "CheckoutResponse",
    "PortalSessionResponse",
    "WebhookAck",
    "WebhookEventListResponse",
    "WebhookEventRecord",
]
