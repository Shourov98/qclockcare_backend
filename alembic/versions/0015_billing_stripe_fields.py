"""Add Stripe identifiers + webhook idempotency table (ADR-0021).

Adds columns to `agencies` that mirror what Stripe Checkout / Subscriptions
write back to us so we can correlate our local subscription state with the
remote Stripe state. None of these columns are user-editable; they're
populated exclusively by webhook handlers (`customer.subscription.*`,
`invoice.payment_*`) running under the SUPER_ADMIN service-role context.

Also creates `stripe_webhook_events` for at-most-once delivery semantics —
Stripe explicitly retries on non-2xx, and we must not re-apply the same
event twice (e.g. flipping the agency from SUSPENDED → ACTIVE twice on a
`subscription.created` + retried `invoice.payment_succeeded`).

Revision ID: 0015_billing_stripe_fields
Revises: 0014_agency_subscription_plans
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0015_billing_stripe_fields"
down_revision: str | Sequence[str] | None = "0014_agency_subscription_plans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ----- agencies: Stripe mirrors -----
    # All nullable because every agency still works as before; the columns
    # only become populated once that agency goes through Stripe checkout.
    op.add_column(
        "agencies",
        sa.Column("stripe_customer_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "agencies",
        sa.Column("stripe_subscription_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "agencies",
        sa.Column("stripe_price_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "agencies",
        sa.Column(
            "current_period_start",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "agencies",
        sa.Column(
            "current_period_end",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "agencies",
        sa.Column(
            "cancel_at_period_end",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "agencies",
        sa.Column(
            "subscription_synced_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    # Stripe customer / subscription IDs MUST be unique when present.
    # Partial unique indexes so multiple agencies without a Stripe link
    # (which is the common pre-payment state) don't conflict.
    op.create_index(
        "uq_agencies_stripe_customer_id",
        "agencies",
        ["stripe_customer_id"],
        unique=True,
        postgresql_where=sa.text("stripe_customer_id IS NOT NULL"),
    )
    op.create_index(
        "uq_agencies_stripe_subscription_id",
        "agencies",
        ["stripe_subscription_id"],
        unique=True,
        postgresql_where=sa.text("stripe_subscription_id IS NOT NULL"),
    )

    # ----- stripe_webhook_events (idempotency) -----
    # Stores every Stripe event we successfully processed. Re-deliveries
    # of the same event short-circuit at the service layer and return
    # 200 without re-mutating agency state.
    op.create_table(
        "stripe_webhook_events",
        sa.Column(
            "stripe_event_id",
            sa.String(length=64),
            primary_key=True,
        ),
        sa.Column(
            "event_type",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "agency_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agencies.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_stripe_webhook_events_event_type",
        "stripe_webhook_events",
        ["event_type"],
    )
    op.create_index(
        "idx_stripe_webhook_events_agency_id",
        "stripe_webhook_events",
        ["agency_id"],
    )
    op.create_index(
        "idx_stripe_webhook_events_processed_at",
        "stripe_webhook_events",
        [sa.text("processed_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_stripe_webhook_events_processed_at",
        table_name="stripe_webhook_events",
    )
    op.drop_index(
        "idx_stripe_webhook_events_agency_id",
        table_name="stripe_webhook_events",
    )
    op.drop_index(
        "idx_stripe_webhook_events_event_type",
        table_name="stripe_webhook_events",
    )
    op.drop_table("stripe_webhook_events")

    op.drop_index(
        "uq_agencies_stripe_subscription_id",
        table_name="agencies",
    )
    op.drop_index(
        "uq_agencies_stripe_customer_id",
        table_name="agencies",
    )

    op.drop_column("agencies", "subscription_synced_at")
    op.drop_column("agencies", "cancel_at_period_end")
    op.drop_column("agencies", "current_period_end")
    op.drop_column("agencies", "current_period_start")
    op.drop_column("agencies", "stripe_price_id")
    op.drop_column("agencies", "stripe_subscription_id")
    op.drop_column("agencies", "stripe_customer_id")
