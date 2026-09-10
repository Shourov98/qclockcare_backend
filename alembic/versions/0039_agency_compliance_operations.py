"""Add tenant-scoped service authorizations, supervisory visits, and reports.

Revision ID: 0039_agency_compliance_operations
Revises: 0038_appointment_payment_lifecycle
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0039_agency_compliance_operations"
down_revision = "0038_appointment_payment_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "service_authorizations",
        sa.Column("id", uuid, primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("agency_id", uuid, sa.ForeignKey("agencies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("patient_id", uuid, sa.ForeignKey("patient_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("program_type", sa.String(64), nullable=False),
        sa.Column("service_name", sa.String(255), nullable=False),
        sa.Column("authorization_number", sa.String(128)),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=False),
        sa.Column("authorized_units", sa.Float(), nullable=False),
        sa.Column("used_units", sa.Float(), server_default="0", nullable=False),
        sa.Column("unit_label", sa.String(32), server_default="hours", nullable=False),
        sa.Column("status", sa.String(16), server_default="ACTIVE", nullable=False),
        sa.Column("notes", sa.Text()),
        sa.CheckConstraint("ends_on >= starts_on", name="ck_service_authorization_dates"),
        sa.CheckConstraint("authorized_units > 0", name="ck_service_authorization_authorized_units"),
        sa.CheckConstraint("used_units >= 0", name="ck_service_authorization_used_units"),
        sa.CheckConstraint("status IN ('ACTIVE', 'EXPIRING', 'EXPIRED', 'EXHAUSTED', 'CANCELLED')", name="ck_service_authorization_status"),
    )
    op.create_index("ix_service_authorizations_agency_id", "service_authorizations", ["agency_id"])
    op.create_index("ix_service_authorizations_patient_id", "service_authorizations", ["patient_id"])
    op.create_index("ix_service_authorizations_ends_on", "service_authorizations", ["ends_on"])
    op.create_index("idx_service_authorizations_agency_status", "service_authorizations", ["agency_id", "status"])

    op.create_table(
        "supervisory_visits",
        sa.Column("id", uuid, primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("agency_id", uuid, sa.ForeignKey("agencies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("staff_id", uuid, sa.ForeignKey("staff_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("patient_id", uuid, sa.ForeignKey("patient_profiles.id", ondelete="SET NULL")),
        sa.Column("supervisor_user_id", uuid, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(16), server_default="SCHEDULED", nullable=False),
        sa.Column("objectives", sa.Text()),
        sa.Column("findings", sa.Text()),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('SCHEDULED', 'COMPLETED', 'CANCELLED')", name="ck_supervisory_visit_status"),
    )
    op.create_index("ix_supervisory_visits_agency_id", "supervisory_visits", ["agency_id"])
    op.create_index("ix_supervisory_visits_staff_id", "supervisory_visits", ["staff_id"])
    op.create_index("ix_supervisory_visits_patient_id", "supervisory_visits", ["patient_id"])
    op.create_index("ix_supervisory_visits_scheduled_at", "supervisory_visits", ["scheduled_at"])
    op.create_index("idx_supervisory_visits_agency_scheduled", "supervisory_visits", ["agency_id", "scheduled_at"])

    op.create_table(
        "agency_compliance_reports",
        sa.Column("id", uuid, primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("agency_id", uuid, sa.ForeignKey("agencies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reporter_user_id", uuid, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("patient_id", uuid, sa.ForeignKey("patient_profiles.id", ondelete="SET NULL")),
        sa.Column("category", sa.String(32), server_default="OTHER", nullable=False),
        sa.Column("severity", sa.String(16), server_default="MEDIUM", nullable=False),
        sa.Column("status", sa.String(20), server_default="OPEN", nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("resolution_note", sa.Text()),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_by_user_id", uuid, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.CheckConstraint("category IN ('DOCUMENTATION', 'STAFF_CREDENTIAL', 'SAFETY', 'SERVICE_AUTH', 'STAFF_TRAINING', 'OTHER')", name="ck_agency_compliance_report_category"),
        sa.CheckConstraint("severity IN ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW')", name="ck_agency_compliance_report_severity"),
        sa.CheckConstraint("status IN ('OPEN', 'IN_PROGRESS', 'RESOLVED', 'DISMISSED')", name="ck_agency_compliance_report_status"),
    )
    op.create_index("ix_agency_compliance_reports_agency_id", "agency_compliance_reports", ["agency_id"])
    op.create_index("ix_agency_compliance_reports_reporter_user_id", "agency_compliance_reports", ["reporter_user_id"])
    op.create_index("ix_agency_compliance_reports_patient_id", "agency_compliance_reports", ["patient_id"])
    op.create_index("ix_agency_compliance_reports_status", "agency_compliance_reports", ["status"])
    op.create_index("idx_agency_compliance_reports_agency_status", "agency_compliance_reports", ["agency_id", "status"])


def downgrade() -> None:
    op.drop_table("agency_compliance_reports")
    op.drop_table("supervisory_visits")
    op.drop_table("service_authorizations")
