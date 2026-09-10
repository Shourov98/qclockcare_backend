"""Store the amount due for each appointment in integer cents.

Revision ID: 0037_appointment_billing_amount
Revises: 0036_group_home_owners
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0037_appointment_billing_amount"
down_revision = "0036_group_home_owners"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "appointments",
        sa.Column(
            "billing_amount_cents",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(
        "ck_appointments_billing_amount_non_negative",
        "appointments",
        "billing_amount_cents >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_appointments_billing_amount_non_negative",
        "appointments",
        type_="check",
    )
    op.drop_column("appointments", "billing_amount_cents")
