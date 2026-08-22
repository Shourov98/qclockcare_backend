"""Add admin_scopes table for cross-tenant PLATFORM_ADMIN RBAC.

SUPER_ADMIN has full cross-tenant access. PLATFORM_ADMIN is a new
role below SUPER_ADMIN that holds one or more scopes (AGENCIES,
CLINICAL, SUPPORT) granting partial cross-tenant access.

This migration creates the join table that maps a PLATFORM_ADMIN user
to their granted scopes. SUPER_ADMIN users do not need rows here —
they always pass scope checks.

Design notes:
  - scope_name is VARCHAR(64) (not a Postgres enum) so adding new
    scopes doesn't require an ALTER TYPE migration.
  - CHECK constraint pins valid values; bad values are rejected at
    the DB level.
  - granted_by tracks who issued the scope; nullable for backward
    compatibility (e.g. seeds that predate this table).
  - RLS is intentionally NOT enabled on this table. It is only read
    in the context of an authenticated request, and the JWT carries
    the scopes. No row-level scoping needed.

Revision ID: 0024_admin_scopes
Revises: 0023_notification_prefs_agency_nullable
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024_admin_scopes"
down_revision = "0023_notification_prefs_agency_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_scopes",
        sa.Column("user_id", sa.dialects.postgresql.UUID(), nullable=False),
        sa.Column("scope_name", sa.String(length=64), nullable=False),
        sa.Column(
            "granted_by",
            sa.dialects.postgresql.UUID(),
            nullable=True,
        ),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["granted_by"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint(
            "user_id", "scope_name", name="pk_admin_scopes"
        ),
        sa.CheckConstraint(
            "scope_name IN ('AGENCIES', 'CLINICAL', 'SUPPORT')",
            name="chk_admin_scopes_scope_name",
        ),
    )
    op.create_index(
        "idx_admin_scopes_scope_name",
        "admin_scopes",
        ["scope_name"],
    )


def downgrade() -> None:
    op.drop_index("idx_admin_scopes_scope_name", table_name="admin_scopes")
    op.drop_table("admin_scopes")
