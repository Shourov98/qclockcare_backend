"""Add billing_status + claim_id + billing_paid_* to appointments.

Denormalizes the visit-level billing toggle onto the appointment row so
the agency-admin dashboard / visit-summary screen can render "Paid" /
"Unpaid" without joining to `visits`. The visit row keeps its own
`billing_confirmed_at` (timestamp of the staff confirmation) — this
migration just promotes the durable state to the appointment.

`claim_id` is auto-generated at insert time by the service layer
(format `CG-{agency_code_short}-{appt_id_short}`). This column just
guarantees uniqueness so a re-seeded appointment can't double up.

Revision ID: 0028_appointment_billing
Revises: 0027_appointment_flow_alignment
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028_appointment_billing"
down_revision = "0027_appointment_flow_alignment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "appointments",
        sa.Column(
            "billing_status",
            sa.String(length=16),
            nullable=False,
            server_default="unpaid",
        ),
    )
    op.add_column(
        "appointments",
        sa.Column(
            "billing_paid_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "appointments",
        sa.Column(
            "billing_paid_by_user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "appointments",
        sa.Column(
            "claim_id",
            sa.String(length=32),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_appointments_billing_status",
        "appointments",
        "billing_status IN ('unpaid', 'paid')",
    )
    op.create_unique_constraint(
        "uq_appointments_claim_id",
        "appointments",
        ["claim_id"],
    )
    op.create_foreign_key(
        "fk_appointments_billing_paid_by_user_id_users",
        "appointments",
        "users",
        ["billing_paid_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "idx_appointments_billing_status",
        "appointments",
        ["billing_status"],
    )


def downgrade() -> None:
    op.drop_index("idx_appointments_billing_status", table_name="appointments")
    op.drop_constraint(
        "fk_appointments_billing_paid_by_user_id_users",
        "appointments",
        type_="foreignkey",
    )
    op.drop_constraint("uq_appointments_claim_id", "appointments", type_="unique")
    op.drop_constraint(
        "ck_appointments_billing_status", "appointments", type_="check"
    )
    op.drop_column("appointments", "claim_id")
    op.drop_column("appointments", "billing_paid_by_user_id")
    op.drop_column("appointments", "billing_paid_at")
    op.drop_column("appointments", "billing_status")