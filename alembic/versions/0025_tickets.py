"""Add tickets + ticket_comments + ticket_code_sequence tables.

Internal admin support tickets, scoped to the platform admin dashboard
(`/admin/tickets`). NOT tenant-scoped — `agency_id` is nullable so
cross-tenant issues (e.g. "Stripe webhooks dropping globally") can be
tracked on a single ticket.

Two enums are added to the database (`ticket_status`, `ticket_priority`,
`ticket_comment_kind`) to match the Python `StrEnum`s in
`src.shared.domain.enums`.

`ticket_code_sequence` keeps a per-day counter so the human-friendly
`TK-XXXX` code is monotonic within a calendar day in UTC and resets at
midnight. The service layer (`_next_ticket_code`) locks the row
`FOR UPDATE` to avoid duplicate codes under concurrent inserts.

Files / attachments are intentionally out of scope for v1 — the UI
shows the attachment column as a count derived from
`ticket_comments.kind = 'ATTACHMENT'` rows. If we ever need real file
uploads we'll add S3 wiring and a separate `ticket_attachments` table.

Revision ID: 0025_tickets
Revises: 0024_admin_scopes
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0025_tickets"
down_revision = "0024_admin_scopes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Enums (created explicitly up-front so we can pass them to the
    # columns without re-emitting CREATE TYPE in the table definitions)
    # ------------------------------------------------------------------
    ticket_status = postgresql.ENUM(
        "OPEN",
        "IN_PROGRESS",
        "PENDING",
        "RESOLVED",
        "CLOSED",
        name="ticket_status",
    )
    ticket_priority = postgresql.ENUM(
        "CRITICAL",
        "HIGH",
        "MEDIUM",
        "LOW",
        name="ticket_priority",
    )
    ticket_comment_kind = postgresql.ENUM(
        "COMMENT",
        "STATUS_CHANGE",
        "ASSIGNMENT",
        "ATTACHMENT",
        name="ticket_comment_kind",
    )
    ticket_status.create(op.get_bind(), checkfirst=True)
    ticket_priority.create(op.get_bind(), checkfirst=True)
    ticket_comment_kind.create(op.get_bind(), checkfirst=True)

    # ------------------------------------------------------------------
    # tickets
    # ------------------------------------------------------------------
    op.create_table(
        "tickets",
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
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
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
            "agency_id",
            sa.dialects.postgresql.UUID(),
            nullable=True,
        ),
        sa.Column(
            "reporter_user_id",
            sa.dialects.postgresql.UUID(),
            nullable=False,
        ),
        sa.Column(
            "assignee_user_id",
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
            ondelete="SET NULL",
            name="fk_tickets_agency_id_agencies",
        ),
        sa.ForeignKeyConstraint(
            ["reporter_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
            name="fk_tickets_reporter_user_id_users",
        ),
        sa.ForeignKeyConstraint(
            ["assignee_user_id"],
            ["users.id"],
            ondelete="SET NULL",
            name="fk_tickets_assignee_user_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tickets"),
        sa.UniqueConstraint("code", name="uq_tickets_code"),
    )
    # Cast the bare-string columns to the ENUM types so Postgres
    # enforces the same constraints as the ORM.
    #
    # The columns were created with VARCHAR + a literal string default
    # (`'OPEN'`, `'MEDIUM'`). Postgres won't auto-cast that literal to
    # the enum type, so we DROP DEFAULT, run the cast, then SET DEFAULT
    # back with an explicit `::ticket_status` cast on the literal.
    op.execute("ALTER TABLE tickets ALTER COLUMN status DROP DEFAULT")
    op.execute(
        "ALTER TABLE tickets ALTER COLUMN status TYPE ticket_status "
        "USING status::ticket_status"
    )
    op.execute(
        "ALTER TABLE tickets ALTER COLUMN status SET DEFAULT 'OPEN'::ticket_status"
    )

    op.execute("ALTER TABLE tickets ALTER COLUMN priority DROP DEFAULT")
    op.execute(
        "ALTER TABLE tickets ALTER COLUMN priority TYPE ticket_priority "
        "USING priority::ticket_priority"
    )
    op.execute(
        "ALTER TABLE tickets ALTER COLUMN priority SET DEFAULT 'MEDIUM'::ticket_priority"
    )
    op.create_index(
        "ix_tickets_status", "tickets", ["status"], unique=False
    )
    op.create_index(
        "ix_tickets_priority", "tickets", ["priority"], unique=False
    )
    op.create_index(
        "ix_tickets_agency_id", "tickets", ["agency_id"], unique=False
    )
    op.create_index(
        "ix_tickets_reporter_user_id",
        "tickets",
        ["reporter_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_tickets_assignee_user_id",
        "tickets",
        ["assignee_user_id"],
        unique=False,
    )
    op.create_index(
        "idx_tickets_status_priority",
        "tickets",
        ["status", "priority"],
        unique=False,
    )
    op.create_index(
        "idx_tickets_created_at",
        "tickets",
        ["created_at"],
        unique=False,
    )

    # ------------------------------------------------------------------
    # ticket_comments
    # ------------------------------------------------------------------
    op.create_table(
        "ticket_comments",
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
            "kind",
            sa.String(length=32),
            nullable=False,
            server_default="COMMENT",
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "event_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "edited_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["tickets.id"],
            ondelete="CASCADE",
            name="fk_ticket_comments_ticket_id_tickets",
        ),
        sa.ForeignKeyConstraint(
            ["author_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
            name="fk_ticket_comments_author_user_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ticket_comments"),
    )
    op.execute("ALTER TABLE ticket_comments ALTER COLUMN kind DROP DEFAULT")
    op.execute(
        "ALTER TABLE ticket_comments ALTER COLUMN kind TYPE ticket_comment_kind "
        "USING kind::ticket_comment_kind"
    )
    op.execute(
        "ALTER TABLE ticket_comments ALTER COLUMN kind SET DEFAULT 'COMMENT'::ticket_comment_kind"
    )
    op.create_index(
        "ix_ticket_comments_ticket_id",
        "ticket_comments",
        ["ticket_id"],
        unique=False,
    )
    op.create_index(
        "idx_ticket_comments_ticket_created",
        "ticket_comments",
        ["ticket_id", "created_at"],
        unique=False,
    )

    # ------------------------------------------------------------------
    # ticket_code_sequence — per-day counter for `TK-XXXX` codes.
    # ------------------------------------------------------------------
    op.create_table(
        "ticket_code_sequence",
        sa.Column("seq_date", sa.Date(), primary_key=True, nullable=False),
        sa.Column(
            "last_value",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_table("ticket_code_sequence")
    op.drop_index("idx_ticket_comments_ticket_created", table_name="ticket_comments")
    op.drop_index("ix_ticket_comments_ticket_id", table_name="ticket_comments")
    op.drop_table("ticket_comments")
    op.drop_index("idx_tickets_created_at", table_name="tickets")
    op.drop_index("idx_tickets_status_priority", table_name="tickets")
    op.drop_index("ix_tickets_assignee_user_id", table_name="tickets")
    op.drop_index("ix_tickets_reporter_user_id", table_name="tickets")
    op.drop_index("ix_tickets_agency_id", table_name="tickets")
    op.drop_index("ix_tickets_priority", table_name="tickets")
    op.drop_index("ix_tickets_status", table_name="tickets")
    op.drop_table("tickets")

    op.execute("DROP TYPE IF EXISTS ticket_comment_kind")
    op.execute("DROP TYPE IF EXISTS ticket_priority")
    op.execute("DROP TYPE IF EXISTS ticket_status")
