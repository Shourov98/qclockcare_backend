"""Staff + qualifications + availability tables (schema doc §6).

Tables:
- staff_profiles         — per-agency staff record (links to users)
- staff_qualifications   — credentials held by a staff member, with optional
                           document_storage_key for S3-backed proof
- staff_availability     — recurring weekly windows + one-off blocks

All three are agency-scoped. RLS policies mirror the `agencies`/`users`
pattern: SUPER_ADMIN full access; AGENCY_ADMIN/STAFF at the same agency
see their own agency's rows. STAFF users see only their own profile /
their own qualifications / their own availability.

Revision ID: 0004_staff
Revises: 0003_tokens
Create Date: 2026-06-27 06:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import CITEXT

# revision identifiers, used by Alembic.
revision: str = "0004_staff"
down_revision: str | Sequence[str] | None = "0003_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ============================================================
    # staff_profiles
    # ============================================================
    op.create_table(
        "staff_profiles",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "agency_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agencies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("staff_code", sa.Text(), nullable=False),
        # status reuses the user_status enum so the lifecycle is uniform
        sa.Column(
            "status",
            postgresql.ENUM(name="user_status", create_type=False),
            nullable=False,
            server_default="INVITED",
        ),
        sa.Column("hired_at", sa.Date(), nullable=True),
        sa.Column("terminated_at", sa.Date(), nullable=True),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("agency_id", "user_id", name="uq_staff_per_user_per_agency"),
        sa.UniqueConstraint("agency_id", "staff_code", name="uq_staff_code_per_agency"),
        sa.CheckConstraint(
            "(terminated_at IS NULL) OR (hired_at IS NULL) OR (terminated_at >= hired_at)",
            name="ck_staff_terminated_after_hired",
        ),
    )
    op.create_index(
        "idx_staff_profiles_agency_id",
        "staff_profiles",
        ["agency_id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "idx_staff_profiles_user_id",
        "staff_profiles",
        ["user_id"],
    )

    # ---- updated_at trigger ----
    op.execute(
        "CREATE TRIGGER trg_staff_profiles_updated_at "
        "BEFORE UPDATE ON staff_profiles "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    # ============================================================
    # staff_qualifications
    # ============================================================
    op.create_table(
        "staff_qualifications",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "staff_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("staff_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agency_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agencies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "qualification_type",
            postgresql.ENUM(name="qualification_type", create_type=False),
            nullable=False,
        ),
        # null = applies to all programs the agency offers
        sa.Column(
            "program_type",
            postgresql.ENUM(name="program_type", create_type=False),
            nullable=True,
        ),
        # S3/Supabase Storage key for the credential document
        sa.Column("document_storage_key", sa.Text(), nullable=True),
        sa.Column("issued_at", sa.Date(), nullable=True),
        sa.Column("expires_at", sa.Date(), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(name="qualification_status", create_type=False),
            nullable=False,
            server_default="PENDING_VERIFICATION",
        ),
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
        sa.CheckConstraint(
            "(expires_at IS NULL) OR (issued_at IS NULL) OR (expires_at >= issued_at)",
            name="ck_qualification_expires_after_issued",
        ),
    )
    op.create_index(
        "idx_staff_qualifications_staff_id",
        "staff_qualifications",
        ["staff_id"],
    )
    op.create_index(
        "idx_staff_qualifications_program",
        "staff_qualifications",
        ["program_type"],
        postgresql_where=sa.text("program_type IS NOT NULL"),
    )
    op.create_index(
        "idx_staff_qualifications_expiring",
        "staff_qualifications",
        ["expires_at"],
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.execute(
        "CREATE TRIGGER trg_staff_qualifications_updated_at "
        "BEFORE UPDATE ON staff_qualifications "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    # ============================================================
    # staff_availability
    # ============================================================
    op.create_table(
        "staff_availability",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "staff_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("staff_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agency_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agencies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "is_unavailable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("day_of_week", sa.SmallInteger(), nullable=True),
        sa.Column("start_time", sa.Time(), nullable=True),
        sa.Column("end_time", sa.Time(), nullable=True),
        sa.Column("specific_date", sa.Date(), nullable=True),
        sa.Column("specific_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("specific_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "(specific_date IS NOT NULL) <> (day_of_week IS NOT NULL)",
            name="ck_availability_recurring_or_specific",
        ),
        sa.CheckConstraint(
            "(specific_end IS NULL OR specific_start IS NULL OR specific_end > specific_start) "
            "AND (end_time IS NULL OR start_time IS NULL OR end_time > start_time)",
            name="ck_availability_end_after_start",
        ),
    )
    op.create_index(
        "idx_staff_availability_staff_id",
        "staff_availability",
        ["staff_id"],
    )
    op.create_index(
        "idx_staff_availability_specific_date",
        "staff_availability",
        ["specific_date"],
        postgresql_where=sa.text("specific_date IS NOT NULL"),
    )

    # ============================================================
    # RLS
    # ============================================================
    for table in ("staff_profiles", "staff_qualifications", "staff_availability"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    # ---- staff_profiles ----
    # Super Admin: all. AGENCY_ADMIN at the agency: all of that agency's
    # staff. The staff member themselves: only their own row.
    op.execute(
        """
        CREATE POLICY staff_profiles_select ON staff_profiles
        FOR SELECT
        USING (
            app.is_super_admin()
            OR user_id = app.current_user_id()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND agency_id = app.current_agency_id()
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY staff_profiles_modify ON staff_profiles
        FOR ALL
        USING (
            app.is_super_admin()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND agency_id = app.current_agency_id()
            )
        )
        WITH CHECK (
            app.is_super_admin()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND agency_id = app.current_agency_id()
            )
        )
        """
    )

    # ---- staff_qualifications ----
    # Super Admin: all. AGENCY_ADMIN at the agency: all of that agency's
    # qualifications. The staff member themselves: their own.
    op.execute(
        """
        CREATE POLICY staff_qualifications_select ON staff_qualifications
        FOR SELECT
        USING (
            app.is_super_admin()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND agency_id = app.current_agency_id()
            )
            OR EXISTS (
                SELECT 1 FROM staff_profiles sp
                WHERE sp.id = staff_qualifications.staff_id
                  AND sp.user_id = app.current_user_id()
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY staff_qualifications_modify ON staff_qualifications
        FOR ALL
        USING (
            app.is_super_admin()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND agency_id = app.current_agency_id()
            )
        )
        WITH CHECK (
            app.is_super_admin()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND agency_id = app.current_agency_id()
            )
        )
        """
    )

    # ---- staff_availability ----
    op.execute(
        """
        CREATE POLICY staff_availability_select ON staff_availability
        FOR SELECT
        USING (
            app.is_super_admin()
            OR EXISTS (
                SELECT 1 FROM staff_profiles sp
                WHERE sp.id = staff_availability.staff_id
                  AND (
                    sp.user_id = app.current_user_id()
                    OR (
                        app.has_agency_role('AGENCY_ADMIN')
                        AND sp.agency_id = app.current_agency_id()
                    )
                  )
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY staff_availability_modify ON staff_availability
        FOR ALL
        USING (
            app.is_super_admin()
            OR EXISTS (
                SELECT 1 FROM staff_profiles sp
                WHERE sp.id = staff_availability.staff_id
                  AND sp.user_id = app.current_user_id()
            )
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND EXISTS (
                    SELECT 1 FROM staff_profiles sp
                    WHERE sp.id = staff_availability.staff_id
                      AND sp.agency_id = app.current_agency_id()
                )
            )
        )
        WITH CHECK (
            app.is_super_admin()
            OR EXISTS (
                SELECT 1 FROM staff_profiles sp
                WHERE sp.id = staff_availability.staff_id
                  AND sp.user_id = app.current_user_id()
            )
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND EXISTS (
                    SELECT 1 FROM staff_profiles sp
                    WHERE sp.id = staff_availability.staff_id
                      AND sp.agency_id = app.current_agency_id()
                )
            )
        )
        """
    )


def downgrade() -> None:
    # Drop policies
    op.execute("DROP POLICY IF EXISTS staff_availability_modify ON staff_availability")
    op.execute("DROP POLICY IF EXISTS staff_availability_select ON staff_availability")
    op.execute("DROP POLICY IF EXISTS staff_qualifications_modify ON staff_qualifications")
    op.execute("DROP POLICY IF EXISTS staff_qualifications_select ON staff_qualifications")
    op.execute("DROP POLICY IF EXISTS staff_profiles_modify ON staff_profiles")
    op.execute("DROP POLICY IF EXISTS staff_profiles_select ON staff_profiles")

    # Drop triggers
    op.execute("DROP TRIGGER IF EXISTS trg_staff_qualifications_updated_at ON staff_qualifications")
    op.execute("DROP TRIGGER IF EXISTS trg_staff_profiles_updated_at ON staff_profiles")

    # Disable RLS before dropping tables
    for table in ("staff_availability", "staff_qualifications", "staff_profiles"):
        op.execute(f"ALTER TABLE IF EXISTS {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE IF EXISTS {table} DISABLE ROW LEVEL SECURITY")

    op.drop_table("staff_availability")
    op.drop_table("staff_qualifications")
    op.drop_table("staff_profiles")