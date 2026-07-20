"""Billing module — Stripe integration (ADR-0021).

This module owns the agency's *paid* subscription lifecycle:
- Creating a Stripe Customer + Checkout session so an AGENCY_ADMIN
  can complete payment.
- Receiving webhooks from Stripe and writing back the resulting
  subscription state onto the `agencies` row.
- Idempotent webhook delivery via `stripe_webhook_events`.

Surfaced endpoints:
  POST  /agencies/{agency_id}/billing/checkout        — start a checkout
  POST  /agencies/{agency_id}/billing/portal-session  — open the billing portal
  POST  /billing/webhook                              — Stripe webhook receiver
  GET   /billing/events                               — debug/replay (SUPER_ADMIN)

The whole module is feature-flagged (`FEATURE_BILLING_ENABLED`). When
the flag is False, every route short-circuits with 503 and the webhook
endpoint refuses to register.
"""

from __future__ import annotations

__all__: list[str] = []
