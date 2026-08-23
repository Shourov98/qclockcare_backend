"""Add agency_documents + agency_licenses tables.

Powers the `/admin/compliance` dashboard. Both tables are scoped by
`agency_id` so RLS can apply (and so AGENCY_ADMIN can read their own
rows once we turn on tenant policies).

Two Postgres ENUMs added: `document_type`, `document_status`,
`license_status`. They mirror the Python `StrEnum`s in
`src.shared.domain.enums` and are registered in
`src.shared.domain.enum_mapping` so SQLAlchemy can find them.

Revision ID: 0026_compliance
Revises: 0025_tickets
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0026_compliance"
down_revision = "0025_tickets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Enums
    # ------------------------------------------------------------------
    document_type = postgresql.ENUM(
        "LICENSE",
        "CERTIFICATE",
        "DOCUMENT",
        "PERMIT",
        "POLICY",
        "REPORT",
        name="document_type",
    )
    document_status = postgresql.ENUM(
        "MISSING",
        "PENDING",
        "VALID",
        "EXPIRING",
        "EXPIRED",
        "REJECTED",
        name="document_status",
    )
    license_status = postgresql.ENUM(
        "VALID",
        "UPCOMING",
        "WARNING",
        "CRITICAL",
        "EXPIRED",
        name="license_status",
    )
    document_type.create(op.get_bind(), checkfirst=True)
    document_status.create(op.get_bind(), checkfirst=True)
    license_status.create(op.get_bind(), checkfirst=True)

    # ------------------------------------------------------------------
    # agency_documents
    # ------------------------------------------------------------------
    op.create_table(
        "agency_documents",
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
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "agency_id",
            sa.dialects.postgresql.UUID(),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("doc_type", sa.String(length=32), nullable=False, server_default="DOCUMENT"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="MISSING"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("file_url", sa.String(length=1024), nullable=True),
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
            name="fk_agency_documents_agency_id_agencies",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agency_documents"),
    )
    # Cast VARCHAR columns to ENUM types. The columns were created with a
    # literal string default ('DOCUMENT', 'MISSING'), so we DROP DEFAULT,
    # cast the column type, then re-attach the default with an explicit
    # `::document_type` / `::document_status` cast so Postgres accepts it.
    op.execute("ALTER TABLE agency_documents ALTER COLUMN doc_type DROP DEFAULT")
    op.execute(
        "ALTER TABLE agency_documents ALTER COLUMN doc_type TYPE document_type "
        "USING doc_type::document_type"
    )
    op.execute(
        "ALTER TABLE agency_documents ALTER COLUMN doc_type "
        "SET DEFAULT 'DOCUMENT'::document_type"
    )
    op.execute("ALTER TABLE agency_documents ALTER COLUMN status DROP DEFAULT")
    op.execute(
        "ALTER TABLE agency_documents ALTER COLUMN status TYPE document_status "
        "USING status::document_status"
    )
    op.execute(
        "ALTER TABLE agency_documents ALTER COLUMN status "
        "SET DEFAULT 'MISSING'::document_status"
    )
    op.create_index(
        "ix_agency_documents_agency_id", "agency_documents", ["agency_id"], unique=False
    )
    op.create_index(
        "ix_agency_documents_doc_type", "agency_documents", ["doc_type"], unique=False
    )
    op.create_index(
        "ix_agency_documents_status", "agency_documents", ["status"], unique=False
    )
    op.create_index(
        "ix_agency_documents_expires_at", "agency_documents", ["expires_at"], unique=False
    )
    op.create_index(
        "idx_agency_documents_agency_status",
        "agency_documents",
        ["agency_id", "status"],
        unique=False,
    )

    # ------------------------------------------------------------------
    # agency_licenses
    # ------------------------------------------------------------------
    op.create_table(
        "agency_licenses",
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
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "agency_id",
            sa.dialects.postgresql.UUID(),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("doc_type", sa.String(length=32), nullable=False, server_default="LICENSE"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="VALID"),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reference_number", sa.String(length=128), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
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
            name="fk_agency_licenses_agency_id_agencies",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agency_licenses"),
    )
    # Same DROP DEFAULT → cast → SET DEFAULT pattern as agency_documents
    # above (see the comment block there for the rationale).
    op.execute("ALTER TABLE agency_licenses ALTER COLUMN doc_type DROP DEFAULT")
    op.execute(
        "ALTER TABLE agency_licenses ALTER COLUMN doc_type TYPE document_type "
        "USING doc_type::document_type"
    )
    op.execute(
        "ALTER TABLE agency_licenses ALTER COLUMN doc_type "
        "SET DEFAULT 'LICENSE'::document_type"
    )
    op.execute("ALTER TABLE agency_licenses ALTER COLUMN status DROP DEFAULT")
    op.execute(
        "ALTER TABLE agency_licenses ALTER COLUMN status TYPE license_status "
        "USING status::license_status"
    )
    op.execute(
        "ALTER TABLE agency_licenses ALTER COLUMN status "
        "SET DEFAULT 'VALID'::license_status"
    )
    op.create_index(
        "ix_agency_licenses_agency_id", "agency_licenses", ["agency_id"], unique=False
    )
    op.create_index(
        "ix_agency_licenses_doc_type", "agency_licenses", ["doc_type"], unique=False
    )
    op.create_index(
        "ix_agency_licenses_status", "agency_licenses", ["status"], unique=False
    )
    op.create_index(
        "ix_agency_licenses_expires_at", "agency_licenses", ["expires_at"], unique=False
    )
    op.create_index(
        "idx_agency_licenses_agency_status",
        "agency_licenses",
        ["agency_id", "status"],
        unique=False,
    )
    op.create_index(
        "idx_agency_licenses_status_expires",
        "agency_licenses",
        ["status", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_agency_licenses_status_expires", table_name="agency_licenses")
    op.drop_index("idx_agency_licenses_agency_status", table_name="agency_licenses")
    op.drop_index("ix_agency_licenses_expires_at", table_name="agency_licenses")
    op.drop_index("ix_agency_licenses_status", table_name="agency_licenses")
    op.drop_index("ix_agency_licenses_doc_type", table_name="agency_licenses")
    op.drop_index("ix_agency_licenses_agency_id", table_name="agency_licenses")
    op.drop_table("agency_licenses")

    op.drop_index("idx_agency_documents_agency_status", table_name="agency_documents")
    op.drop_index("ix_agency_documents_expires_at", table_name="agency_documents")
    op.drop_index("ix_agency_documents_status", table_name="agency_documents")
    op.drop_index("ix_agency_documents_doc_type", table_name="agency_documents")
    op.drop_index("ix_agency_documents_agency_id", table_name="agency_documents")
    op.drop_table("agency_documents")

    op.execute("DROP TYPE IF EXISTS license_status")
    op.execute("DROP TYPE IF EXISTS document_status")
    op.execute("DROP TYPE IF EXISTS document_type")