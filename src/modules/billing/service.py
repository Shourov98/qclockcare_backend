"""Billing service — checkout + portal endpoints.

Keeps Stripe SDK calls out of the router (which already has enough to
do with auth, validation, and error mapping). Each public function maps
to one endpoint and is fully covered by an integration test.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import Settings
from src.core.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from src.modules.agencies.models import Agency
from src.modules.billing.schemas import CheckoutRequest, CheckoutResponse, PortalSessionResponse
from src.modules.billing.stripe_service import BillingService, CheckoutResult
from src.modules.identity.dependencies import CurrentAuth
from src.shared.domain.enums import AgencyStatus


# --------------------------------------------------------------------------
# Authorization
# --------------------------------------------------------------------------
def _authorize_actor(actor: CurrentAuth, agency: Agency) -> None:
    """Caller must either be SUPER_ADMIN or the agency's own AGENCY_ADMIN.

    Other roles (STAFF, PATIENT, GUARDIAN) have no business initiating
    billing flows — they shouldn't even know the endpoint exists, but
    we belt-and-brace the check.
    """
    from src.shared.domain.enums import UserRole

    if actor.role == UserRole.SUPER_ADMIN:
        return
    if actor.role == UserRole.AGENCY_ADMIN and actor.agency_id == agency.id:
        return
    raise ForbiddenError(
        "Only the agency's AGENCY_ADMIN or a SUPER_ADMIN can manage billing.",
    )


# --------------------------------------------------------------------------
# Checkout
# --------------------------------------------------------------------------
async def start_checkout(
    session: AsyncSession,
    *,
    settings: Settings,
    actor: CurrentAuth,
    agency_id: uuid.UUID,
    payload: CheckoutRequest,
) -> CheckoutResponse:
    """Begin a Stripe Checkout session for the agency.

    The agency's Stripe Customer is created on demand (idempotent) so
    the same agency never ends up with multiple Customer records.
    """
    agency = await _get_active_agency(session, agency_id=agency_id)
    _authorize_actor(actor, agency)

    if agency.status in {AgencyStatus.SUSPENDED, AgencyStatus.CHURNED}:
        raise ConflictError(
            f"Agency is {agency.status.value}; cannot start a new subscription.",
            details={"agency_status": agency.status.value},
        )
    if agency.deleted_at is not None:
        raise ConflictError("Agency is soft-deleted.")

    admin_email = actor.user.email
    if not admin_email:
        raise ValidationError(
            "Auth context missing email; cannot create Stripe customer.",
        )

    billing = BillingService(settings)
    customer_id = billing.get_or_create_customer(
        agency_id=str(agency.id),
        existing_customer_id=agency.stripe_customer_id,
        admin_email=admin_email,
    )
    if agency.stripe_customer_id != customer_id:
        agency.stripe_customer_id = customer_id

    success_url = payload.success_url or settings.STRIPE_CHECKOUT_SUCCESS_URL
    cancel_url = payload.cancel_url or settings.STRIPE_CHECKOUT_CANCEL_URL
    result: CheckoutResult = billing.create_checkout_session(
        agency_id=str(agency.id),
        customer_id=customer_id,
        plan=payload.plan,
        success_url=success_url,
        cancel_url=cancel_url,
    )
    await session.flush()
    return CheckoutResponse(checkout_url=result.url, session_id=result.session_id)


# --------------------------------------------------------------------------
# Billing portal
# --------------------------------------------------------------------------
async def start_portal_session(
    session: AsyncSession,
    *,
    settings: Settings,
    actor: CurrentAuth,
    agency_id: uuid.UUID,
) -> PortalSessionResponse:
    """Open the Stripe-hosted billing portal for the agency admin."""
    agency = await _get_active_agency(session, agency_id=agency_id)
    _authorize_actor(actor, agency)
    if not agency.stripe_customer_id:
        raise ValidationError(
            "Agency has no Stripe customer yet — start checkout first.",
            details={"agency_id": str(agency.id)},
        )

    billing = BillingService(settings)
    portal_url = billing.create_portal_session(
        customer_id=agency.stripe_customer_id,
        return_url=settings.STRIPE_CHECKOUT_SUCCESS_URL,
    )
    return PortalSessionResponse(portal_url=portal_url)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
async def _get_active_agency(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
) -> Agency:
    agency = (
        await session.execute(
            select(Agency).where(
                Agency.id == agency_id,
                Agency.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if agency is None:
        raise NotFoundError(details={"resource": "agency", "id": str(agency_id)})
    return agency


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "start_checkout",
    "start_portal_session",
]


# Re-export to keep `from src.modules.billing.service import …` clean.
_ = Any  # silence linters when `Any` is otherwise unused
