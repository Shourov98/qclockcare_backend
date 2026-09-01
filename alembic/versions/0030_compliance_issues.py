"""Add `compliance_issues` table for the admin issue queue.

Public surface:
  GET    /admin/compliance/issues
  GET    /admin/compliance/issues/stats
  POST   /admin/compliance/issues
  GET    /admin/compliance/issues/{id}
  PATCH  /admin/compliance/issues/{id}
  POST   /admin/compliance/issues/{id}/resolve
  POST   /admin/compliance/issues/{id}/dismiss
  POST   /admin/compliance/issues/{id}/assign
  DELETE /admin/compliance/issues/{id}

The admin FE renders the Compliance Issue Queue widget
(`farhan-salad-admin/components/compliance/ComplianceIssueQueueTable.tsx`)
with hardcoded mock data — this migration adds the persistent table so
the widget can be backed by real CRUD.

Revision ID: 0030_compliance_issues
Revises: 0029_help_support_tickets
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0030_compliance_issues"
down_revision = "0029_help_support_tickets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Enums (created explicitly up-front so we can pass them to the
    # columns without re-emitting CREATE TYPE in the table definitions).
    # ------------------------------------------------------------------
    compliance_issue_severity = postgresql.ENUM(
        "CRITICAL",
        "HIGH",
        "MEDIUM",
        "LOW",
        name="compliance_issue_severity",
    )
    compliance_issue_status = postgresql.ENUM(
        "OPEN",
        "IN_PROGRESS",
        "PENDING_REVIEW",
        "RESOLVED",
        "DISMISSED",
        name="compliance_issue_status",
    )
    compliance_issue_category = postgresql.ENUM(
        "DOCUMENTATION",
        "STAFF_CREDENTIAL",
        "SAFETY",
        "SERVICE_AUTH",
        "STAFF_TRAINING",
        "OTHER",
        name="compliance_issue_category",
    )
    compliance_issue_severity.create(op.get_bind(), checkfirst=True)
    compliance_issue_status.create(op.get_bind(), checkfirst=True)
    compliance_issue_category.create(op.get_bind(), checkfirst=True)

    # ------------------------------------------------------------------
    # compliance_issues
    # ------------------------------------------------------------------
    op.create_table(
        "compliance_issues",
        sa.Column("id", sa.dialects.postgresql.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "agency_id",
            sa.dialects.postgresql.UUID(),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        # Bare-string columns at create-time; cast to the Postgres ENUMs
        # below so future inserts are constrained to the declared labels.
        sa.Column(
            "severity",
            sa.String(length=32),
            nullable=False,
            server_default="MEDIUM",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="OPEN",
        ),
        sa.Column(
            "category",
            sa.String(length=32),
            nullable=False,
            server_default="OTHER",
        ),
        sa.Column(
            "reporter_user_id",
            sa.dialects.postgresql.UUID(),
            nullable=True,
        ),
        sa.Column(
            "assignee_user_id",
            sa.dialects.postgresql.UUID(),
            nullable=True,
        ),
        sa.Column(
            "due_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "resolved_by_user_id",
            sa.dialects.postgresql.UUID(),
            nullable=True,
        ),
        sa.Column(
            "linked_entity_type",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column(
            "linked_entity_id",
            sa.dialects.postgresql.UUID(),
            nullable=True,
        ),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "extra",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.ForeignKeyConstraint(
            ["agency_id"],
            ["agencies.id"],
            ondelete="CASCADE",
            name="fk_compliance_issues_agency_id_agencies",
        ),
        sa.ForeignKeyConstraint(
            ["reporter_user_id"],
            ["users.id"],
            ondelete="SET NULL",
            name="fk_compliance_issues_reporter_user_id_users",
        ),
        sa.ForeignKeyConstraint(
            ["assignee_user_id"],
            ["users.id"],
            ondelete="SET NULL",
            name="fk_compliance_issues_assignee_user_id_users",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by_user_id"],
            ["users.id"],
            ondelete="SET NULL",
            name="fk_compliance_issues_resolved_by_user_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_compliance_issues"),
    )

    # Cast bare-string columns to the Postgres ENUM types.
    op.execute("ALTER TABLE compliance_issues ALTER COLUMN severity DROP DEFAULT")
    op.execute(
        "ALTER TABLE compliance_issues ALTER COLUMN severity "
        "TYPE compliance_issue_severity "
        "USING severity::compliance_issue_severity"
    )
    op.execute(
        "ALTER TABLE compliance_issues ALTER COLUMN severity "
        "SET DEFAULT 'MEDIUM'::compliance_issue_severity"
    )
    op.execute("ALTER TABLE compliance_issues ALTER COLUMN status DROP DEFAULT")
    op.execute(
        "ALTER TABLE compliance_issues ALTER COLUMN status "
        "TYPE compliance_issue_status "
        "USING status::compliance_issue_status"
    )
    op.execute(
        "ALTER TABLE compliance_issues ALTER COLUMN status "
        "SET DEFAULT 'OPEN'::compliance_issue_status"
    )
    op.execute("ALTER TABLE compliance_issues ALTER COLUMN category DROP DEFAULT")
    op.execute(
        "ALTER TABLE compliance_issues ALTER COLUMN category "
        "TYPE compliance_issue_category "
        "USING category::compliance_issue_category"
    )
    op.execute(
        "ALTER TABLE compliance_issues ALTER COLUMN category "
        "SET DEFAULT 'OTHER'::compliance_issue_category"
    )

    # ------------------------------------------------------------------
    # Indexes
    # ------------------------------------------------------------------
    op.create_index(
        "ix_compliance_issues_agency_id", "compliance_issues", ["agency_id"]
    )
    op.create_index(
        "ix_compliance_issues_reporter_user_id",
        "compliance_issues",
        ["reporter_user_id"],
    )
    op.create_index(
        "ix_compliance_issues_assignee_user_id",
        "compliance_issues",
        ["assignee_user_id"],
    )
    op.create_index(
        "ix_compliance_issues_resolved_by_user_id",
        "compliance_issues",
        ["resolved_by_user_id"],
    )
    # Dashboard list (filter by status/severity + sort by created_at desc)
    op.create_index(
        "idx_compliance_issues_agency_status",
        "compliance_issues",
        ["agency_id", "status"],
    )
    op.create_index(
        "idx_compliance_issues_agency_severity",
        "compliance_issues",
        ["agency_id", "severity"],
    )
    op.create_index(
        "idx_compliance_issues_agency_due_at",
        "compliance_issues",
        ["agency_id", "due_at"],
    )
    op.create_index(
        "idx_compliance_issues_agency_created",
        "compliance_issues",
        ["agency_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_compliance_issues_agency_created", table_name="compliance_issues"
    )
    op.drop_index(
        "idx_compliance_issues_agency_due_at", table_name="compliance_issues"
    )
    op.drop_index(
        "idx_compliance_issues_agency_severity", table_name="compliance_issues"
    )
    op.drop_index(
        "idx_compliance_issues_agency_status", table_name="compliance_issues"
    )
    op.drop_index(
        "ix_compliance_issues_resolved_by_user_id",
        table_name="compliance_issues",
    )
    op.drop_index(
        "ix_compliance_issues_assignee_user_id", table_name="compliance_issues"
    )
    op.drop_index(
        "ix_compliance_issues_reporter_user_id", table_name="compliance_issues"
    )
    op.drop_index("ix_compliance_issues_agency_id", table_name="compliance_issues")
    op.drop_table("compliance_issues")

    op.execute("DROP TYPE IF EXISTS compliance_issue_category")
    op.execute("DROP TYPE IF EXISTS compliance_issue_status")
    op.execute("DROP TYPE IF EXISTS compliance_issue_severity")