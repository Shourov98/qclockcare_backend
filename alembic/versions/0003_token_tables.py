"""Token tables — refresh + single-use tokens (ADR-0016 §7.2, §7.3).

Why two tables?
- `refresh_tokens` — long-lived (days). One row per issued refresh token.
  Revocation = set `revoked_at`. Logout-everywhere = revoke all rows for a user.
- `single_use_tokens` — invitation + password-reset tokens. Each row has a
  `purpose` discriminator; revocation happens by setting `consumed_at` (or
  `revoked_at` if we want to invalidate before consumption).

Both tables have RLS enabled:
- refresh_tokens: users can only see their own rows; service-role inserts.
- single_use_tokens: same shape.

We use the same `app.current_user_id()` helper as 0002. The INSERT path goes
through the application (which sets the GUCs as part of the issuing request),
so insert-side RLS evaluation sees the user context.

Revision ID: 0003_tokens
Revises: 0002_rls
Create Date: 2026-06-27 04:30:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_tokens"
down_revision: str | Sequence[str] | None = "0002_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- refresh_tokens ----
    op.create_table(
        "refresh_tokens",
        sa.Column(
            "jti",
            sa.Text(),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "revoked_reason",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "user_agent",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "ip_address",
            sa.dialects.postgresql.INET(),
            nullable=True,
        ),
    )
    op.create_index("idx_refresh_tokens_user_active", "refresh_tokens", ["user_id", "expires_at"])
    op.execute(
        "CREATE INDEX idx_refresh_tokens_user_active_unrevoked "
        "ON refresh_tokens (user_id, expires_at) "
        "WHERE revoked_at IS NULL"
    )
    op.execute(
        "CREATE INDEX idx_refresh_tokens_expires_active "
        "ON refresh_tokens (expires_at) "
        "WHERE revoked_at IS NULL"
    )

    # ---- single_use_tokens ----
    # Purpose values mirror the JWT "purpose" claim; kept as text + CHECK so
    # we don't have to ship a new enum migration when we add a new flow.
    op.create_table(
        "single_use_tokens",
        sa.Column(
            "jti",
            sa.Text(),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "purpose",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "consumed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.CheckConstraint(
            "purpose IN ('invitation', 'password_reset')",
            name="ck_single_use_tokens_purpose",
        ),
    )
    op.create_index("idx_single_use_tokens_user_purpose", "single_use_tokens", ["user_id", "purpose"])
    op.execute(
        "CREATE INDEX idx_single_use_tokens_lookup "
        "ON single_use_tokens (jti) "
        "WHERE consumed_at IS NULL AND revoked_at IS NULL"
    )

    # ---- RLS ----
    for table in ("refresh_tokens", "single_use_tokens"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    # refresh_tokens: a user sees their own rows; service paths insert/revoke
    # via SECURITY DEFINER-style access (the issuing request sets the GUCs).
    op.execute(
        """
        CREATE POLICY refresh_tokens_select ON refresh_tokens
        FOR SELECT
        USING (
            app.is_super_admin()
            OR user_id = app.current_user_id()
        )
        """
    )
    op.execute(
        """
        CREATE POLICY refresh_tokens_modify ON refresh_tokens
        FOR ALL
        USING (
            app.is_super_admin()
            OR user_id = app.current_user_id()
        )
        WITH CHECK (
            app.is_super_admin()
            OR user_id = app.current_user_id()
        )
        """
    )

    # single_use_tokens: same shape. Users can't enumerate them by querying —
    # the service fetches by exact jti after extracting it from the inbound
    # token. The SELECT policy allows self-lookup just for symmetry.
    op.execute(
        """
        CREATE POLICY single_use_tokens_select ON single_use_tokens
        FOR SELECT
        USING (
            app.is_super_admin()
            OR user_id = app.current_user_id()
        )
        """
    )
    op.execute(
        """
        CREATE POLICY single_use_tokens_modify ON single_use_tokens
        FOR ALL
        USING (
            app.is_super_admin()
            OR user_id = app.current_user_id()
        )
        WITH CHECK (
            app.is_super_admin()
            OR user_id = app.current_user_id()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS single_use_tokens_modify ON single_use_tokens")
    op.execute("DROP POLICY IF EXISTS single_use_tokens_select ON single_use_tokens")
    op.execute("DROP POLICY IF EXISTS refresh_tokens_modify ON refresh_tokens")
    op.execute("DROP POLICY IF EXISTS refresh_tokens_select ON refresh_tokens")

    op.execute("ALTER TABLE IF EXISTS single_use_tokens DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE IF EXISTS refresh_tokens DISABLE ROW LEVEL SECURITY")

    op.drop_table("single_use_tokens")
    op.drop_table("refresh_tokens")