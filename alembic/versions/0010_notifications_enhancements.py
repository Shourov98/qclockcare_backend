"""Notifications enhancements — preferences + multi-channel deliveries.

Adds:
  - notification_preferences: per (user, type, channel) opt-in/opt-out.
    Default = opted-in for all combos; rows are lazy-seeded at first read.
  - notification_deliveries: per (notification, channel) attempt log.
    Phase 1 covers IN_APP (the existing channel) + EMAIL (real SMTP when
    SMTP_ENABLED=true). SMS is a stub provider that logs and returns
    success unless SMS_ENABLED=true (then it raises NotImplementedError).

RLS:
  - notification_preferences SELECT/INSERT/UPDATE/DELETE: scoped to the
    owner (user_id = app.current_user_id()). AGENCY_ADMIN may SELECT
    per-agency preferences (for ops dashboards), but cannot modify.
  - notification_deliveries SELECT: notification's recipient sees their
    own; AGENCY_ADMIN sees all in their agency; SUPER_ADMIN sees all.
    INSERT: only via dispatcher (service-layer RLS context, or with
    bypass — for Phase 1 we INSERT via the same session that wrote the
    notification row, so recipient-context RLS applies). UPDATE: dispatcher
    updates status as providers ack; SUPER_ADMIN bypass.

Revision ID: 0010_notifications_enhancements
Revises: 0009_audit_logs
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0010_notifications_enhancements"
down_revision: str | Sequence[str] | None = "0009_audit_logs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ============================================================
    # notification_preferences
    # ============================================================
    op.create_table(
        "notification_preferences",
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "agency_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agencies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "type",
            postgresql.ENUM(name="notification_type", create_type=False),
            primary_key=True,
        ),
        sa.Column(
            "channel",
            postgresql.ENUM(name="notification_channel", create_type=False),
            primary_key=True,
        ),
        sa.Column(
            "opted_in",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "opted_in IN (true, false)",
            name="ck_notification_prefs_opted_in_bool",
        ),
    )
    op.create_index(
        "idx_notification_prefs_user",
        "notification_preferences",
        ["user_id"],
    )
    op.create_index(
        "idx_notification_prefs_agency",
        "notification_preferences",
        ["agency_id", "type"],
    )

    # ============================================================
    # notification_deliveries
    # ============================================================
    op.create_table(
        "notification_deliveries",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "notification_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("notifications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agency_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agencies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "channel",
            postgresql.ENUM(name="notification_channel", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(name="notification_status", create_type=False),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column(
            "provider_message_id",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "error",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "delivered_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "notification_id",
            "channel",
            name="uq_notification_deliveries_notification_channel",
        ),
    )
    op.create_index(
        "idx_notification_deliveries_notification",
        "notification_deliveries",
        ["notification_id"],
    )
    op.create_index(
        "idx_notification_deliveries_agency_status",
        "notification_deliveries",
        ["agency_id", "status", sa.text("created_at DESC")],
    )

    # ============================================================
    # RLS — preferences
    # ============================================================
    op.execute("ALTER TABLE notification_preferences ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE notification_preferences FORCE ROW LEVEL SECURITY")

    op.execute(
        """
        CREATE POLICY notification_prefs_owner_all
        ON notification_preferences
        FOR ALL
        USING (user_id = app.current_user_id())
        WITH CHECK (user_id = app.current_user_id())
        """
    )
    op.execute(
        """
        CREATE POLICY notification_prefs_admin_select
        ON notification_preferences
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

    # ============================================================
    # RLS — deliveries
    # ============================================================
    op.execute("ALTER TABLE notification_deliveries ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE notification_deliveries FORCE ROW LEVEL SECURITY")

    op.execute(
        """
        CREATE POLICY notification_deliveries_select
        ON notification_deliveries
        FOR SELECT
        USING (
            app.is_super_admin()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND agency_id = app.current_agency_id()
            )
            OR EXISTS (
                SELECT 1 FROM notifications n
                WHERE n.id = notification_deliveries.notification_id
                  AND n.recipient_user_id = app.current_user_id()
            )
        )
        """
    )
    # Insert/update is intentionally narrow — only the dispatcher (running
    # in the same session as the notification insert) should write here.
    # The service layer uses the recipient's session, so this requires the
    # notification row's recipient to be the current user, OR SUPER_ADMIN
    # bypass for AGENCY_ADMIN fan-out (not currently used; kept for future).
    op.execute(
        """
        CREATE POLICY notification_deliveries_insert
        ON notification_deliveries
        FOR INSERT
        WITH CHECK (
            app.is_super_admin()
            OR EXISTS (
                SELECT 1 FROM notifications n
                WHERE n.id = notification_deliveries.notification_id
                  AND n.recipient_user_id = app.current_user_id()
            )
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND agency_id = app.current_agency_id()
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY notification_deliveries_update
        ON notification_deliveries
        FOR UPDATE
        USING (
            app.is_super_admin()
            OR EXISTS (
                SELECT 1 FROM notifications n
                WHERE n.id = notification_deliveries.notification_id
                  AND n.recipient_user_id = app.current_user_id()
            )
        )
        """
    )


def downgrade() -> None:
    # deliveries
    op.execute("DROP POLICY IF EXISTS notification_deliveries_update ON notification_deliveries")
    op.execute("DROP POLICY IF EXISTS notification_deliveries_insert ON notification_deliveries")
    op.execute("DROP POLICY IF EXISTS notification_deliveries_select ON notification_deliveries")
    op.drop_table("notification_deliveries")

    # preferences
    op.execute("DROP POLICY IF EXISTS notification_prefs_admin_select ON notification_preferences")
    op.execute("DROP POLICY IF EXISTS notification_prefs_owner_all ON notification_preferences")
    op.drop_table("notification_preferences")
