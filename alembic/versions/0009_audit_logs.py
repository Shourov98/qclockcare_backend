"""Audit logs table (schema doc §14).

Append-only log of business actions (not auth events — those go to
`auth_audit_events`, see migration 0003). One row per logical action.

Enforcement:
- DB trigger `audit_logs_no_modify` blocks UPDATE + DELETE so the log
  is append-only at the storage layer.
- RLS: SELECT for SUPER_ADMIN + AGENCY_ADMIN scoped to their agency;
  INSERT for AGENCY_ADMIN (writes only — no UPDATE/DELETE via API).
  The service-layer helper runs as the caller's session, which already
  has the agency GUC set.

Indexes:
- (agency_id, created_at DESC) — primary list path
- (actor_user_id, created_at DESC) — per-actor history
- (entity_type, entity_id, created_at DESC) — per-entity history
- (action, created_at DESC) — per-action history

Enum `audit_action` already exists from migration 0001; reused via
`create_type=False`.

Revision ID: 0009_audit_logs
Revises: 0008_notifications
Create Date: 2026-06-29 10:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0009_audit_logs"
down_revision: str | Sequence[str] | None = "0008_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ============================================================
    # audit_logs
    # ============================================================
    op.create_table(
        "audit_logs",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        # Nullable so SUPER_ADMIN cross-agency actions can be logged
        # without claiming a specific agency.
        sa.Column(
            "agency_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agencies.id", ondelete="CASCADE"),
            nullable=True,
        ),
        # Nullable so we can log events where the actor is a system
        # process or an unauthenticated request (e.g. LOGIN_FAILED).
        sa.Column(
            "actor_user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "action",
            postgresql.ENUM(name="audit_action", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "entity_type",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "entity_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "old_data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "new_data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "ip_address",
            postgresql.INET(),
            nullable=True,
        ),
        sa.Column(
            "user_agent",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "entity_type <> '' AND length(trim(entity_type)) > 0",
            name="ck_audit_logs_entity_type_non_empty",
        ),
    )
    op.create_index(
        "idx_audit_logs_agency_date",
        "audit_logs",
        ["agency_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_audit_logs_actor",
        "audit_logs",
        ["actor_user_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_audit_logs_entity",
        "audit_logs",
        ["entity_type", "entity_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_audit_logs_action",
        "audit_logs",
        ["action", sa.text("created_at DESC")],
    )

    # ============================================================
    # Append-only enforcement
    # ============================================================
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_logs_no_modify()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_logs is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_logs_no_modify
        BEFORE UPDATE OR DELETE ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION audit_logs_no_modify()
        """
    )

    # ============================================================
    # RLS — enable + force
    # ============================================================
    op.execute("ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_logs FORCE ROW LEVEL SECURITY")

    # SELECT: SUPER_ADMIN sees everything; AGENCY_ADMIN sees their agency.
    # No self-modify path — writes go through the service helper.
    op.execute(
        """
        CREATE POLICY audit_logs_select ON audit_logs
        FOR SELECT
        USING (
            app.is_super_admin()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND agency_id = app.current_agency_id()
            )
        )
        """
    )
    # INSERT only — UPDATE and DELETE are blocked by the trigger and
    # the policy allows INSERT only (not ALL).
    op.execute(
        """
        CREATE POLICY audit_logs_insert ON audit_logs
        FOR INSERT
        WITH CHECK (
            app.is_super_admin()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND (
                    agency_id = app.current_agency_id()
                    OR agency_id IS NULL
                )
            )
            -- The application can also insert via the service helper
            -- running in a request with AGENCY_ADMIN at any agency.
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_logs_no_modify ON audit_logs")
    op.execute("DROP FUNCTION IF EXISTS audit_logs_no_modify()")
    op.execute("DROP POLICY IF EXISTS audit_logs_insert ON audit_logs")
    op.execute("DROP POLICY IF EXISTS audit_logs_select ON audit_logs")
    op.drop_table("audit_logs")