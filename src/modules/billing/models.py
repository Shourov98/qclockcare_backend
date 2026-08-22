"""Billing module — ORM models.

The only table this module owns is `stripe_webhook_events`, used for
at-most-once delivery of webhook handlers. The Stripe-mirror columns
(`stripe_customer_id`, etc.) live on `agencies` — see the agencies module
to keep all agency mutation in one place.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.domain.base_entity import Base


class StripeWebhookEvent(Base):
    """One Stripe webhook delivery we've successfully processed.

    Primary-keyed by `stripe_event_id` so the webhook handler short-circuits
    duplicate deliveries (Stripe retries on non-2xx, and we've seen DLQ-style
    redeliveries where the same event hits us minutes apart). Insert in the
    same transaction as the agency mutation so a crash midway never produces
    a half-applied state.
    """

    __tablename__ = "stripe_webhook_events"

    stripe_event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    agency_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id", ondelete="SET NULL"),
        nullable=True,
    )
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False)

    __table_args__ = (
        Index("idx_stripe_webhook_events_event_type", "event_type"),
        Index("idx_stripe_webhook_events_agency_id", "agency_id"),
        Index(
            "idx_stripe_webhook_events_processed_at",
            processed_at.desc(),
        ),
    )


__all__ = ["StripeWebhookEvent"]
