"""Add agency-scoped conversations, participants, and messages.

Revision ID: 0043_messaging
Revises: 0042_sync_audit_action_enum
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0043_messaging"
down_revision = "0042_sync_audit_action_enum"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("agency_id", postgresql.UUID(), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_message_preview", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["agency_id"], ["agencies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_conversations_agency_id", "conversations", ["agency_id"])
    op.create_index("idx_conversations_agency_last_message", "conversations", ["agency_id", "last_message_at"])
    op.create_table(
        "conversation_participants",
        sa.Column("id", postgresql.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(), nullable=False),
        sa.Column("user_id", postgresql.UUID(), nullable=False),
        sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", "user_id", name="uq_conversation_participant"),
    )
    op.create_index("idx_conversation_participants_user", "conversation_participants", ["user_id"])
    op.create_table(
        "conversation_messages",
        sa.Column("id", postgresql.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(), nullable=False),
        sa.Column("sender_user_id", postgresql.UUID(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sender_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_conversation_messages_conversation_id", "conversation_messages", ["conversation_id"])
    op.create_index("idx_conversation_messages_conversation_created", "conversation_messages", ["conversation_id", "created_at"])

    # Tenant isolation is enforced again in the database. Service-layer
    # relationship checks narrow visibility further for patient/guardian/staff.
    op.execute("ALTER TABLE conversations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE conversation_participants ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE conversation_messages ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE OR REPLACE FUNCTION app.is_conversation_participant(target_conversation_id uuid)
        RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = public, app
        AS $$
            SELECT EXISTS (
                SELECT 1 FROM conversation_participants
                WHERE conversation_id = target_conversation_id
                  AND user_id = app.current_user_id()
            )
        $$
    """)
    op.execute("""
        CREATE POLICY conversations_member_select ON conversations FOR SELECT
        USING (
            agency_id = app.current_agency_id()
            AND app.is_conversation_participant(conversations.id)
        )
    """)
    op.execute("""
        CREATE POLICY conversations_create ON conversations FOR INSERT
        WITH CHECK (agency_id = app.current_agency_id() AND created_by_user_id = app.current_user_id())
    """)
    op.execute("""
        CREATE POLICY conversations_member_update ON conversations FOR UPDATE
        USING (
            agency_id = app.current_agency_id()
            AND app.is_conversation_participant(conversations.id)
        )
        WITH CHECK (agency_id = app.current_agency_id())
    """)
    op.execute("""
        CREATE POLICY participants_member_select ON conversation_participants FOR SELECT
        USING (app.is_conversation_participant(conversation_participants.conversation_id))
    """)
    op.execute("""
        CREATE POLICY participants_creator_insert ON conversation_participants FOR INSERT
        WITH CHECK (EXISTS (SELECT 1 FROM conversations c WHERE c.id = conversation_participants.conversation_id AND c.agency_id = app.current_agency_id() AND c.created_by_user_id = app.current_user_id()))
    """)
    op.execute("""
        CREATE POLICY participants_self_update ON conversation_participants FOR UPDATE
        USING (user_id = app.current_user_id()) WITH CHECK (user_id = app.current_user_id())
    """)
    op.execute("""
        CREATE POLICY messages_member_select ON conversation_messages FOR SELECT
        USING (EXISTS (SELECT 1 FROM conversations c WHERE c.id = conversation_messages.conversation_id AND c.agency_id = app.current_agency_id() AND app.is_conversation_participant(c.id)))
    """)
    op.execute("""
        CREATE POLICY messages_member_insert ON conversation_messages FOR INSERT
        WITH CHECK (sender_user_id = app.current_user_id() AND EXISTS (SELECT 1 FROM conversations c WHERE c.id = conversation_messages.conversation_id AND c.agency_id = app.current_agency_id() AND app.is_conversation_participant(c.id)))
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS app.is_conversation_participant(uuid)")
    op.drop_table("conversation_messages")
    op.drop_table("conversation_participants")
    op.drop_table("conversations")
