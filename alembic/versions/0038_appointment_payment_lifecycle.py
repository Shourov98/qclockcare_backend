"""Replace the legacy appointment billing toggle with a payment lifecycle.

Revision ID: 0038_appointment_payment_lifecycle
Revises: 0037_appointment_billing_amount
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0038_appointment_payment_lifecycle"
down_revision = "0037_appointment_billing_amount"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_appointments_billing_status", "appointments", type_="check")
    op.execute("UPDATE appointments SET billing_status = 'pending' WHERE billing_status = 'unpaid'")
    op.execute("""
        UPDATE appointments
        SET billing_status = 'cancelled', billing_paid_at = NULL, billing_paid_by_user_id = NULL
        WHERE status IN ('CANCELLED', 'MISSED')
    """)
    op.alter_column("appointments", "billing_status", existing_type=sa.String(length=16), server_default="pending")
    op.create_check_constraint("ck_appointments_billing_status", "appointments", "billing_status IN ('pending', 'paid', 'cancelled')")


def downgrade() -> None:
    op.drop_constraint("ck_appointments_billing_status", "appointments", type_="check")
    op.execute("UPDATE appointments SET billing_status = 'unpaid' WHERE billing_status IN ('pending', 'cancelled')")
    op.alter_column("appointments", "billing_status", existing_type=sa.String(length=16), server_default="unpaid")
    op.create_check_constraint("ck_appointments_billing_status", "appointments", "billing_status IN ('unpaid', 'paid')")
