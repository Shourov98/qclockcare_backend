"""Billing router.

Mounts two routers:

  * `/agencies/{agency_id}/billing/*`  — checkout + portal (auth required)
  * `/billing/*`                       — webhook receiver (no auth; uses
                                          Stripe-Signature verification)

Both are gated on `FEATURE_BILLING_ENABLED` per request — a 503 is
returned when billing is off, but the routers stay mounted so the
OpenAPI schema advertises the routes consistently across environments.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.database import get_session
from src.core.exceptions import ServiceUnavailableError
from src.core.logging import get_logger
from src.modules.billing import service as billing_service
from src.modules.billing import webhook_service
from src.modules.billing.schemas import (
    CheckoutRequest,
    CheckoutResponse,
    PortalSessionResponse,
    WebhookAck,
)
from src.modules.billing.stripe_service import (
    StripeSignatureInvalidError,
    build_billing_service,
)
from src.modules.identity.dependencies import (
    CurrentAuth,
    get_session_with_auth,
)
from src.shared.schemas.docs import standard_responses

log = get_logger(__name__)


def _billing_enabled_or_503() -> None:
    """Per-request gate. Lets dev boot without Stripe but refuses calls."""
    if not settings.FEATURE_BILLING_ENABLED:
        raise ServiceUnavailableError(
            "Billing is disabled. Set FEATURE_BILLING_ENABLED=true.",
        )


# --------------------------------------------------------------------------
# Routers — always mounted so OpenAPI advertises the surface
# --------------------------------------------------------------------------
agencies_billing_router = APIRouter(
    prefix="/agencies/{agency_id}/billing",
    tags=["billing"],
)
webhook_router = APIRouter(prefix="/billing", tags=["billing"])


# --------------------------------------------------------------------------
# POST /agencies/{agency_id}/billing/checkout
# --------------------------------------------------------------------------
@agencies_billing_router.post(
    "/checkout",
    response_model=CheckoutResponse,
    status_code=status.HTTP_201_CREATED,
    responses=standard_responses(
        include=[401, 403, 404, 409, 422],
        extras={
            503: {
                "model": WebhookAck,  # placeholder; 503 has no body
                "description": (
                    "Billing is disabled (FEATURE_BILLING_ENABLED=false) "
                    "or Stripe is misconfigured (missing STRIPE_SECRET_KEY "
                    "/ STRIPE_PRICE_*). `code` is `SERVICE_UNAVAILABLE` or "
                    "`BILLING_ERROR`."
                ),
            },
        },
    ),
    summary="Open Stripe Checkout for an agency",
)
async def post_checkout(
    agency_id: uuid.UUID,
    payload: CheckoutRequest,
    auth: CurrentAuth,
    session: AsyncSession = Depends(get_session_with_auth),
) -> CheckoutResponse:
    """Start a Stripe Checkout session for the chosen plan.

    Returns a `checkout_url` the frontend should `window.location`-assign
    the user to. Stripe completes the rest of the flow and POSTs back to
    `/billing/webhook` on success.
    """
    _billing_enabled_or_503()
    return await billing_service.start_checkout(
        session,
        settings=settings,
        actor=auth,
        agency_id=agency_id,
        payload=payload,
    )


# --------------------------------------------------------------------------
# POST /agencies/{agency_id}/billing/portal-session
# --------------------------------------------------------------------------
@agencies_billing_router.post(
    "/portal-session",
    response_model=PortalSessionResponse,
    responses=standard_responses(
        include=[401, 403, 404, 422],
        extras={
            503: {
                "model": PortalSessionResponse,
                "description": "Billing disabled or Stripe misconfigured.",
            },
        },
    ),
    summary="Open the Stripe billing portal for an agency",
)
async def post_portal_session(
    agency_id: uuid.UUID,
    auth: CurrentAuth,
    session: AsyncSession = Depends(get_session_with_auth),
) -> PortalSessionResponse:
    """Open the Stripe-hosted billing portal (manage payment method,
    view invoices, cancel)."""
    _billing_enabled_or_503()
    return await billing_service.start_portal_session(
        session,
        settings=settings,
        actor=auth,
        agency_id=agency_id,
    )


# --------------------------------------------------------------------------
# POST /billing/webhook — Stripe receiver (unauthenticated)
# --------------------------------------------------------------------------
@webhook_router.post(
    "/webhook",
    response_model=WebhookAck,
    status_code=status.HTTP_200_OK,
    # No auth dependency — Stripe signs the request, we verify below.
    responses={
        200: {"description": "Event accepted (or duplicate)."},
        400: {"description": "Missing or invalid Stripe-Signature header."},
        503: {"description": "Billing subsystem misconfigured."},
    },
    summary="Stripe webhook receiver",
)
async def post_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
    session: AsyncSession = Depends(get_session),
) -> WebhookAck | Response:
    """Receive a Stripe webhook delivery.

    Stripe will retry on non-2xx, so we MUST return 2xx for every event
    we successfully process *including* duplicates. Signature failure
    returns 400 — those are forged/invalid and should not be retried
    with the same payload.
    """
    _billing_enabled_or_503()
    raw = await request.body()
    billing = build_billing_service(settings)
    try:
        event = billing.verify_webhook(
            payload=raw,
            signature_header=stripe_signature,
        )
    except StripeSignatureInvalidError:
        return Response(status_code=400)

    outcome = await webhook_service.handle_stripe_event(
        session,
        event=event,
        stripe_price_map=settings.stripe_price_id_for,
    )
    await session.commit()
    log.info(
        "stripe.webhook.handled",
        event_id=event.get("id"),
        event_type=event.get("type"),
        applied=outcome.applied,
        duplicate=outcome.duplicate,
        note=outcome.note,
    )
    return WebhookAck(
        event_id=event.get("id") or "",
        event_type=event.get("type") or "unknown",
        duplicate=outcome.duplicate,
    )


__all__ = [
    "agencies_billing_router",
    "webhook_router",
]
