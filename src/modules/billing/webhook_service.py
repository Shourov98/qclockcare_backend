"""Webhook handler — translate Stripe events into agency mutations.

Stripe webhooks are *retried on non-2xx*, so we must be:

1. **Idempotent**: re-delivery of the same `event_id` MUST be a no-op.
   We rely on `stripe_webhook_events.stripe_event_id` (PK) to short-circuit
   duplicates at the DB level.
2. **Transactional**: every mutation of `agencies` is committed together
   with the `INSERT` into `stripe_webhook_events`. A crash mid-handler
   rolls back both, and Stripe's retry will succeed.
3. **Tolerant**: if the referenced agency no longer exists (deleted
   before the webhook landed), we still return 200 so Stripe stops
   retrying — the event is recorded as orphaned.

Event mapping:

    checkout.session.completed         → stamp stripe_subscription_id on agency
    customer.subscription.created      → ACTIVE, set period + synced_at
    customer.subscription.updated      → update period + cancel_at_period_end
    customer.subscription.deleted      → CHURNED, clear Stripe IDs
    invoice.payment_failed             → SUSPENDED, log churned_at
    invoice.payment_succeeded          → ACTIVE (recovery from failed state)

Unknown event types are accepted (recorded + 200) so we can add new
handlers later without breaking replay.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.logging import get_logger
from src.modules.agencies.models import Agency
from src.modules.billing.models import StripeWebhookEvent
from src.shared.domain.enums import AgencyStatus, AgencySubscriptionPlan

log = get_logger(__name__)


@dataclass(slots=True)
class WebhookOutcome:
    """What the handler decided for a given event."""

    duplicate: bool
    applied: bool
    agency_id: uuid.UUID | None
    note: str


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _to_dt(epoch: int | None) -> datetime | None:
    """Convert a Stripe unix epoch (seconds) into a tz-aware UTC datetime."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=UTC)


async def _resolve_agency(
    session: AsyncSession,
    *,
    customer_id: str | None,
    subscription_id: str | None,
    metadata_agency_id: str | None,
) -> Agency | None:
    """Find the local agency row from the bits Stripe gives us.

    Priority: `metadata.agency_id` (set at checkout time) >
    `customer_id` lookup > `subscription_id` lookup. Returns None if
    nothing matched — the caller logs and stores the event as orphaned.
    """
    if metadata_agency_id:
        try:
            agency_uuid = uuid.UUID(metadata_agency_id)
        except ValueError:
            return None
        agency = (
            await session.execute(select(Agency).where(Agency.id == agency_uuid))
        ).scalar_one_or_none()
        if agency is not None:
            return agency

    if customer_id:
        agency = (
            await session.execute(
                select(Agency).where(Agency.stripe_customer_id == customer_id)
            )
        ).scalar_one_or_none()
        if agency is not None:
            return agency

    if subscription_id:
        agency = (
            await session.execute(
                select(Agency).where(
                    Agency.stripe_subscription_id == subscription_id,
                )
            )
        ).scalar_one_or_none()
        if agency is not None:
            return agency

    return None


def _plan_from_price_id(
    settings_price_map: dict[str, str | None],
    price_id: str | None,
) -> AgencySubscriptionPlan | None:
    if not price_id:
        return None
    for plan_value, configured in settings_price_map.items():
        if configured == price_id:
            try:
                return AgencySubscriptionPlan(plan_value)
            except ValueError:
                return None
    return None


# --------------------------------------------------------------------------
# Handler dispatch
# --------------------------------------------------------------------------
async def handle_stripe_event(
    session: AsyncSession,
    *,
    event: dict[str, Any],
    stripe_price_map: dict[str, str | None],
) -> WebhookOutcome:
    """Process one verified Stripe event.

    Returns the outcome (mostly for logging + tests). The caller is
    responsible for HTTP response — this function never raises for
    expected business outcomes (duplicate, orphan).
    """
    event_id = event.get("id") or ""
    event_type = event.get("type") or "unknown"
    data = (event.get("data") or {}).get("object") or {}
    now = datetime.now(UTC)

    if not event_id:
        return WebhookOutcome(
            duplicate=False,
            applied=False,
            agency_id=None,
            note="event missing id",
        )

    # Pre-flight: short-circuit duplicates by attempting the PK insert
    # inside the same transaction as the agency mutation. If the PK
    # already exists, IntegrityError tells us we've seen this event.
    try:
        session.add(
            _new_event_row(
                event_id=event_id,
                event_type=event_type,
                agency_id=None,  # patched below if we resolve one
                payload=event,
                processed_at=now,
            )
        )
        await session.flush()
    except IntegrityError:
        await session.rollback()
        log.info(
            "stripe.webhook.duplicate",
            event_id=event_id,
            event_type=event_type,
        )
        return WebhookOutcome(
            duplicate=True,
            applied=False,
            agency_id=None,
            note="duplicate",
        )

    agency = await _resolve_agency(
        session,
        customer_id=data.get("customer"),
        subscription_id=data.get("subscription") or data.get("id"),
        metadata_agency_id=(
            (data.get("metadata") or {}).get("agency_id")
            or (event.get("data", {}).get("object", {}).get("metadata") or {}).get("agency_id")
        ),
    )

    if event_type == "checkout.session.completed":
        _apply_checkout_completed(agency, data, stripe_price_map)
        note = "checkout completed"
    elif event_type == "customer.subscription.created":
        _apply_subscription_created_or_updated(
            agency,
            data,
            stripe_price_map,
            is_created=True,
        )
        note = "subscription created"
    elif event_type == "customer.subscription.updated":
        _apply_subscription_created_or_updated(
            agency,
            data,
            stripe_price_map,
            is_created=False,
        )
        note = "subscription updated"
    elif event_type == "customer.subscription.deleted":
        _apply_subscription_deleted(agency)
        note = "subscription deleted"
    elif event_type == "invoice.payment_failed":
        _apply_payment_failed(agency)
        note = "payment failed"
    elif event_type == "invoice.payment_succeeded":
        _apply_payment_succeeded(agency)
        note = "payment succeeded"
    else:
        # Recorded but not applied — return 200 so Stripe stops retrying.
        note = f"unhandled event type: {event_type}"
        log.info("stripe.webhook.unhandled", event_type=event_type)

    if agency is not None:
        agency.subscription_synced_at = now
        # Stamp the agency_id onto the audit row.
        # (We add a fresh row rather than mutate because the session is
        # mid-flush and we don't want to race the constraint.)
        # The IntegrityError guard above already reserved the PK; we
        # can safely UPDATE here.
        await session.execute(
            update(StripeWebhookEvent)
            .where(StripeWebhookEvent.stripe_event_id == event_id)
            .values(agency_id=agency.id)
        )

    return WebhookOutcome(
        duplicate=False,
        applied=agency is not None,
        agency_id=agency.id if agency is not None else None,
        note=note,
    )


def _new_event_row(
    *,
    event_id: str,
    event_type: str,
    agency_id: uuid.UUID | None,
    payload: dict[str, Any],
    processed_at: datetime,
) -> StripeWebhookEvent:
    """Construct the audit row."""
    return StripeWebhookEvent(
        stripe_event_id=event_id,
        event_type=event_type,
        agency_id=agency_id,
        processed_at=processed_at,
        payload=payload,
    )


# --------------------------------------------------------------------------
# Per-event-type mutations
# --------------------------------------------------------------------------
def _apply_checkout_completed(
    agency: Agency | None,
    data: dict[str, Any],
    stripe_price_map: dict[str, str | None],
) -> None:
    """`checkout.session.completed` — Stripe has just charged the customer.

    We stamp the resulting subscription ID onto the agency and switch
    status from TRIAL → ACTIVE (if they were trialing). The next webhook
    (`customer.subscription.created`) refines the period/cancel fields.
    """
    if agency is None:
        return
    subscription_id = data.get("subscription")
    if subscription_id and not agency.stripe_subscription_id:
        agency.stripe_subscription_id = subscription_id
    if agency.status == AgencyStatus.TRIAL:
        agency.status = AgencyStatus.ACTIVE
    plan = _plan_from_price_id(stripe_price_map, data.get("metadata", {}).get("price_id"))
    if plan is not None:
        agency.subscription_plan = plan


def _apply_subscription_created_or_updated(
    agency: Agency | None,
    data: dict[str, Any],
    stripe_price_map: dict[str, str | None],
    *,
    is_created: bool,
) -> None:
    """Mirror a Stripe Subscription object onto our agency row.

    Pulled fields:
      - stripe_price_id
      - subscription_plan (derived from price_id)
      - current_period_start / _end
      - cancel_at_period_end
      - status (mapped to our enum)
    """
    if agency is None:
        return
    items = (data.get("items") or {}).get("data") or []
    price_id = items[0].get("price", {}).get("id") if items else data.get("price", {}).get("id")
    if price_id:
        agency.stripe_price_id = price_id
        plan = _plan_from_price_id(stripe_price_map, price_id)
        if plan is not None:
            agency.subscription_plan = plan
    agency.current_period_start = _to_dt(data.get("current_period_start"))
    agency.current_period_end = _to_dt(data.get("current_period_end"))
    agency.cancel_at_period_end = bool(data.get("cancel_at_period_end"))
    # Stripe status → our AgencyStatus.
    stripe_status = data.get("status")
    if stripe_status == "active":
        agency.status = AgencyStatus.ACTIVE
    elif stripe_status == "trialing":
        agency.status = AgencyStatus.TRIAL
    elif stripe_status in {"past_due", "unpaid", "incomplete"}:
        # past_due is recoverable on next retry — keep ACTIVE so users
        # can still use the product. unpaid/incomplete → SUSPENDED.
        if stripe_status in {"unpaid", "incomplete"}:
            agency.status = AgencyStatus.SUSPENDED
    elif stripe_status == "canceled":
        agency.status = AgencyStatus.CHURNED
    # `is_created` is informational here; future logic might use it
    # (e.g. emit a "subscription created" audit event).
    _ = is_created


def _apply_subscription_deleted(agency: Agency | None) -> None:
    """Customer cancelled (immediate or at period end)."""
    if agency is None:
        return
    agency.status = AgencyStatus.CHURNED
    agency.stripe_subscription_id = None
    agency.stripe_price_id = None
    agency.current_period_start = None
    agency.current_period_end = None
    agency.cancel_at_period_end = False


def _apply_payment_failed(agency: Agency | None) -> None:
    """Subscription renewal charge failed.

    Per the AGENCY_SUSPENDED contract, we lock the agency out the moment
    Stripe tells us the charge didn't go through. The agency's
    `settings.churned_at` carries the timestamp so audit log readers
    can trace the timeline.
    """
    if agency is None:
        return
    agency.status = AgencyStatus.SUSPENDED
    settings = dict(agency.settings or {})
    settings.setdefault(
        "payment_failed_at",
        datetime.now(UTC).isoformat(),
    )
    agency.settings = settings


def _apply_payment_succeeded(agency: Agency | None) -> None:
    """A previously-failed invoice cleared (dunning recovered)."""
    if agency is None:
        return
    if agency.status == AgencyStatus.SUSPENDED:
        agency.status = AgencyStatus.ACTIVE
    settings = dict(agency.settings or {})
    settings.pop("payment_failed_at", None)
    agency.settings = settings


__all__ = ["WebhookOutcome", "handle_stripe_event"]
