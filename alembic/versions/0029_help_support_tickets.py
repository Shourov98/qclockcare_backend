"""Add `support_tickets` + `support_ticket_messages` for patient/guardian ↔ AGENCY_ADMIN help-desk.

Public surface:
  - `POST   /portal/support/tickets`               — patient/guardian opens ticket
  - `GET    /portal/support/tickets`               — patient/guardian lists own
  - `GET    /portal/support/tickets/{id}`          — patient/guardian reads
  - `POST   /portal/support/tickets/{id}/messages` — patient/guardian replies
  - `GET    /agency/support/tickets`               — AGENCY_ADMIN inbox
  - `GET    /agency/support/tickets/{id}`          — AGENCY_ADMIN reads
  - `POST   /agency/support/tickets/{id}/messages` — AGENCY_ADMIN replies
  - `PATCH  /agency/support/tickets/{id}/status`   — AGENCY_ADMIN status/priority change

Distinct from the existing internal-admin `tickets` module at
`/admin/tickets` (which uses `TicketStatus` / `TicketPriority` /
`TicketCommentKind`). New enums `SupportTicketStatus` /
`SupportTicketPriority` / `SupportTicketAuthorKind` keep the public
surface naming consistent (`OPEN`/`AWAITING_REPLY` rather than
the older `PENDING` half-state) and add the author-kind column
needed for the agency inbox feed.

Revision ID: 0029_help_support_tickets
Revises: 0028_appointment_billing
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0029_help_support_tickets"
down_revision = "0028_appointment_billing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Enums (created explicitly up-front so we can pass them to the
    # columns without re-emitting CREATE TYPE in the table definitions).
    # ------------------------------------------------------------------
    support_ticket_status = postgresql.ENUM(
        "OPEN",
        "AWAITING_REPLY",
        "RESOLVED",
        "CLOSED",
        name="support_ticket_status",
    )
    support_ticket_priority = postgresql.ENUM(
        "LOW",
        "MEDIUM",
        "HIGH",
        "URGENT",
        name="support_ticket_priority",
    )
    support_ticket_author_kind = postgresql.ENUM(
        "PATIENT",
        "GUARDIAN",
        "AGENCY_ADMIN",
        name="support_ticket_author_kind",
    )
    support_ticket_status.create(op.get_bind(), checkfirst=True)
    support_ticket_priority.create(op.get_bind(), checkfirst=True)
    support_ticket_author_kind.create(op.get_bind(), checkfirst=True)

    # ------------------------------------------------------------------
    # support_tickets
    # ------------------------------------------------------------------
    op.create_table(
        "support_tickets",
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
        sa.Column(
            "patient_id",
            sa.dialects.postgresql.UUID(),
            nullable=True,
        ),
        sa.Column(
            "reporter_user_id",
            sa.dialects.postgresql.UUID(),
            nullable=False,
        ),
        sa.Column(
            "subject",
            sa.String(length=255),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="OPEN",
        ),
        sa.Column(
            "priority",
            sa.String(length=32),
            nullable=False,
            server_default="MEDIUM",
        ),
        sa.Column(
            "reporter_kind",
            sa.String(length=32),
            nullable=False,
        ),
        sa.Column(
            "last_message_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "last_message_by_user_id",
            sa.dialects.postgresql.UUID(),
            nullable=True,
        ),
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "closed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["agency_id"],
            ["agencies.id"],
            ondelete="CASCADE",
            name="fk_support_tickets_agency_id_agencies",
        ),
        sa.ForeignKeyConstraint(
            ["patient_id"],
            ["patients.id"],
            ondelete="SET NULL",
            name="fk_support_tickets_patient_id_patients",
        ),
        sa.ForeignKeyConstraint(
            ["reporter_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
            name="fk_support_tickets_reporter_user_id_users",
        ),
        sa.ForeignKeyConstraint(
            ["last_message_by_user_id"],
            ["users.id"],
            ondelete="SET NULL",
            name="fk_support_tickets_last_message_by_user_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_support_tickets"),
    )
    # Cast bare-string columns to the Postgres ENUM types so the
    # backend can't insert labels outside the declared set.
    op.execute("ALTER TABLE support_tickets ALTER COLUMN status DROP DEFAULT")
    op.execute(
        "ALTER TABLE support_tickets ALTER COLUMN status TYPE support_ticket_status "
        "USING status::support_ticket_status"
    )
    op.execute(
        "ALTER TABLE support_tickets ALTER COLUMN status "
        "SET DEFAULT 'OPEN'::support_ticket_status"
    )
    op.execute("ALTER TABLE support_tickets ALTER COLUMN priority DROP DEFAULT")
    op.execute(
        "ALTER TABLE support_tickets ALTER COLUMN priority TYPE support_ticket_priority "
        "USING priority::support_ticket_priority"
    )
    op.execute(
        "ALTER TABLE support_tickets ALTER COLUMN priority "
        "SET DEFAULT 'MEDIUM'::support_ticket_priority"
    )
    op.execute(
        "ALTER TABLE support_tickets ALTER COLUMN reporter_kind "
        "TYPE support_ticket_author_kind "
        "USING reporter_kind::support_ticket_author_kind"
    )

    op.create_index(
        "ix_support_tickets_agency_id", "support_tickets", ["agency_id"]
    )
    op.create_index(
        "ix_support_tickets_patient_id", "support_tickets", ["patient_id"]
    )
    op.create_index(
        "ix_support_tickets_reporter_user_id",
        "support_tickets",
        ["reporter_user_id"],
    )
    op.create_index(
        "ix_support_tickets_last_message_by_user_id",
        "support_tickets",
        ["last_message_by_user_id"],
    )
    # Inbox sort: agency_admin opens the dashboard and wants the
    # most-recently-active tickets first.
    op.create_index(
        "idx_support_tickets_agency_status_last_msg",
        "support_tickets",
        ["agency_id", "status", "last_message_at"],
    )
    # Reporter view: "my tickets" sort by created_at desc.
    op.create_index(
        "idx_support_tickets_reporter_created",
        "support_tickets",
        ["reporter_user_id", "created_at"],
    )

    # ------------------------------------------------------------------
    # support_ticket_messages
    # ------------------------------------------------------------------
    op.create_table(
        "support_ticket_messages",
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
            "ticket_id",
            sa.dialects.postgresql.UUID(),
            nullable=False,
        ),
        sa.Column(
            "author_user_id",
            sa.dialects.postgresql.UUID(),
            nullable=False,
        ),
        sa.Column(
            "author_kind",
            sa.String(length=32),
            nullable=False,
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["support_tickets.id"],
            ondelete="CASCADE",
            name="fk_support_ticket_messages_ticket_id_support_tickets",
        ),
        sa.ForeignKeyConstraint(
            ["author_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
            name="fk_support_ticket_messages_author_user_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_support_ticket_messages"),
    )
    op.execute(
        "ALTER TABLE support_ticket_messages ALTER COLUMN author_kind "
        "TYPE support_ticket_author_kind "
        "USING author_kind::support_ticket_author_kind"
    )
    op.create_index(
        "ix_support_ticket_messages_ticket_id",
        "support_ticket_messages",
        ["ticket_id"],
    )
    op.create_index(
        "idx_support_ticket_messages_ticket_created",
        "support_ticket_messages",
        ["ticket_id", "created_at"],
    )
    op.create_index(
        "ix_support_ticket_messages_author_user_id",
        "support_ticket_messages",
        ["author_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_support_ticket_messages_author_user_id",
        table_name="support_ticket_messages",
    )
    op.drop_index(
        "idx_support_ticket_messages_ticket_created",
        table_name="support_ticket_messages",
    )
    op.drop_index(
        "ix_support_ticket_messages_ticket_id",
        table_name="support_ticket_messages",
    )
    op.drop_table("support_ticket_messages")

    op.drop_index(
        "idx_support_tickets_reporter_created", table_name="support_tickets"
    )
    op.drop_index(
        "idx_support_tickets_agency_status_last_msg", table_name="support_tickets"
    )
    op.drop_index(
        "ix_support_tickets_last_message_by_user_id", table_name="support_tickets"
    )
    op.drop_index(
        "ix_support_tickets_reporter_user_id", table_name="support_tickets"
    )
    op.drop_index("ix_support_tickets_patient_id", table_name="support_tickets")
    op.drop_index("ix_support_tickets_agency_id", table_name="support_tickets")
    op.drop_table("support_tickets")

    op.execute("DROP TYPE IF EXISTS support_ticket_author_kind")
    op.execute("DROP TYPE IF EXISTS support_ticket_priority")
    op.execute("DROP TYPE IF EXISTS support_ticket_status")
