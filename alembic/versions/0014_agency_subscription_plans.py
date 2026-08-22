"""Add agency subscription package fields.

Revision ID: 0014_agency_subscription_plans
Revises: 0013_audit_action_read
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0014_agency_subscription_plans"
down_revision: str | Sequence[str] | None = "0013_audit_action_read"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE TYPE agency_subscription_plan AS ENUM "
        "('BASIC', 'PROFESSIONAL', 'ENTERPRISE')"
    )
    op.add_column(
        "agencies",
        sa.Column(
            "subscription_plan",
            postgresql.ENUM(name="agency_subscription_plan", create_type=False),
            nullable=False,
            server_default="BASIC",
        ),
    )
    op.add_column(
        "agencies",
        sa.Column(
            "subscription_price_cents",
            sa.Integer(),
            nullable=False,
            server_default="2900",
        ),
    )
    op.add_column(
        "agencies",
        sa.Column(
            "subscription_billing_cycle",
            sa.String(length=32),
            nullable=False,
            server_default="MONTHLY",
        ),
    )
    op.add_column(
        "agencies",
        sa.Column("trial_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "agencies",
        sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_agencies_subscription_plan",
        "agencies",
        ["subscription_plan"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_check_constraint(
        "ck_agencies_subscription_price_non_negative",
        "agencies",
        "subscription_price_cents >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_agencies_subscription_price_non_negative",
        "agencies",
        type_="check",
    )
    op.drop_index("idx_agencies_subscription_plan", table_name="agencies")
    op.drop_column("agencies", "trial_ends_at")
    op.drop_column("agencies", "trial_started_at")
    op.drop_column("agencies", "subscription_billing_cycle")
    op.drop_column("agencies", "subscription_price_cents")
    op.drop_column("agencies", "subscription_plan")
    op.execute("DROP TYPE agency_subscription_plan")
