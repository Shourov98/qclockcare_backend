"""Notifications table (schema doc §13).

The single `notifications` table covers the in-app channel for Phase 1.
Per-channel delivery tracking (`notification_deliveries`) is intentionally
deferred until email/SMS/push are added — at that point this migration
will get a sibling for that table.

Table:
- notifications — one row per (recipient, event). The `type` column
  encodes the event (APPOINTMENT_CONFIRMED, SERVICE_VERIFIED, etc.)
  and `metadata` (jsonb) carries the linked entity IDs and any
  display data the client needs.

Status lifecycle: PENDING → SENT → (DELIVERED|READ) or FAILED.
For the in-app channel, "delivery" is instantaneous so the row goes
straight to SENT, then to READ when the user marks it read.

RLS:
- SELECT:  SUPER_ADMIN full; AGENCY_ADMIN at agency; recipient_user_id
           = current user.
- MODIFY:  SUPER_ADMIN full; AGENCY_ADMIN at agency (for fan-out
           rewrites); recipient can mark their own as read.

Enums (`notification_type`, `notification_status`, `notification_channel`)
already exist from migration 0001; this migration reuses them via
`create_type=False`.

Revision ID: 0008_notifications
Revises: 0007_visit_verification
Create Date: 2026-06-28 09:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0008_notifications"
down_revision: str | Sequence[str] | None = "0007_visit_verification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ============================================================
    # notifications
    # ============================================================
    op.create_table(
        "notifications",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "agency_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agencies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "recipient_user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "type",
            postgresql.ENUM(name="notification_type", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "title",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "body",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(name="notification_status", create_type=False),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "read_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.CheckConstraint(
            "title <> '' AND length(trim(title)) > 0",
            name="ck_notifications_title_non_empty",
        ),
        sa.CheckConstraint(
            "body <> '' AND length(trim(body)) > 0",
            name="ck_notifications_body_non_empty",
        ),
        sa.CheckConstraint(
            "(read_at IS NULL) OR (status = 'READ')",
            name="ck_notifications_read_at_implies_status_read",
        ),
        sa.CheckConstraint(
            "(status <> 'READ') OR (read_at IS NOT NULL)",
            name="ck_notifications_status_read_implies_read_at",
        ),
    )
    op.create_index(
        "idx_notifications_recipient_unread",
        "notifications",
        ["recipient_user_id", sa.text("created_at DESC")],
        postgresql_where=sa.text("read_at IS NULL AND status <> 'FAILED'"),
    )
    op.create_index(
        "idx_notifications_recipient",
        "notifications",
        ["recipient_user_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_notifications_agency_type",
        "notifications",
        ["agency_id", "type", sa.text("created_at DESC")],
    )

    # ============================================================
    # RLS — enable + force
    # ============================================================
    op.execute("ALTER TABLE notifications ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE notifications FORCE ROW LEVEL SECURITY")

    op.execute(
        """
        CREATE POLICY notifications_select ON notifications
        FOR SELECT
        USING (
            app.is_super_admin()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND agency_id = app.current_agency_id()
            )
            OR recipient_user_id = app.current_user_id()
        )
        """
    )
    op.execute(
        """
        CREATE POLICY notifications_modify ON notifications
        FOR ALL
        USING (
            app.is_super_admin()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND agency_id = app.current_agency_id()
            )
            -- Recipients can mark their own notifications as read.
            OR (
                recipient_user_id = app.current_user_id()
                -- Only UPDATE allowed for self (RLS doesn't differentiate
                -- UPDATE vs INSERT vs DELETE here, so the service layer
                -- restricts the operation by NOT exposing DELETE/INSERT
                -- endpoints for self). Service-layer code enforces the
                -- mark-read-only semantics.
                AND status IN ('PENDING', 'SENT', 'DELIVERED', 'READ')
            )
        )
        WITH CHECK (
            app.is_super_admin()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND agency_id = app.current_agency_id()
            )
            OR (
                recipient_user_id = app.current_user_id()
                AND status IN ('PENDING', 'SENT', 'DELIVERED', 'READ')
            )
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS notifications_modify ON notifications")
    op.execute("DROP POLICY IF EXISTS notifications_select ON notifications")
    op.drop_table("notifications")
