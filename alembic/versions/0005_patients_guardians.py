"""Patient + guardian + relationship tables (schema doc §7).

Tables:
- patient_profiles                 — care-recipient profile at one agency
- guardian_profiles                — person authorised to act on a patient's behalf
- patient_guardian_relationships   — many-to-many patient ↔ guardian with
                                     relationship_type and legal_authority flag

All three are agency-scoped. RLS follows the same pattern as `staff_*`:
SUPER_ADMIN full; AGENCY_ADMIN at the agency sees / manages everything;
the patient themselves sees their own profile + their own relationships;
the guardian sees the guardian profiles they're linked to.

Revision ID: 0005_patients_guardians
Revises: 0004_staff
Create Date: 2026-06-27 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005_patients_guardians"
down_revision: str | Sequence[str] | None = "0004_staff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ============================================================
    # patient_profiles
    # ============================================================
    op.create_table(
        "patient_profiles",
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
        sa.Column("patient_code", sa.Text(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="user_status", create_type=False),
            nullable=False,
            server_default="INVITED",
        ),
        # Demographic / clinical metadata
        sa.Column("date_of_birth", sa.Date(), nullable=True),
        sa.Column("gender", sa.Text(), nullable=True),
        sa.Column("preferred_language", sa.Text(), nullable=True),
        # Free-form care notes (kept here in MVP; later: dedicated care_notes table)
        sa.Column("care_notes", sa.Text(), nullable=True),
        # Enrolment dates
        sa.Column("admitted_at", sa.Date(), nullable=True),
        sa.Column("discharged_at", sa.Date(), nullable=True),
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
        sa.UniqueConstraint("agency_id", "user_id", name="uq_patient_per_user_per_agency"),
        sa.UniqueConstraint("agency_id", "patient_code", name="uq_patient_code_per_agency"),
        sa.CheckConstraint(
            "(discharged_at IS NULL) OR (admitted_at IS NULL) OR (discharged_at >= admitted_at)",
            name="ck_patient_discharged_after_admitted",
        ),
    )
    op.create_index(
        "idx_patient_profiles_agency_id",
        "patient_profiles",
        ["agency_id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "idx_patient_profiles_user_id",
        "patient_profiles",
        ["user_id"],
    )
    op.execute(
        "CREATE TRIGGER trg_patient_profiles_updated_at "
        "BEFORE UPDATE ON patient_profiles "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    # ============================================================
    # guardian_profiles
    # ============================================================
    # A guardian is someone authorised to act on a patient's behalf
    # (spouse, parent, conservator, caseworker, etc.). They have their
    # own `users` row + agency role (GUARDIAN), same pattern as patient.
    op.create_table(
        "guardian_profiles",
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
        sa.Column(
            "status",
            postgresql.ENUM(name="user_status", create_type=False),
            nullable=False,
            server_default="INVITED",
        ),
        # Phone / email may differ from the user's account; this is the
        # contact info to reach the guardian.
        sa.Column("contact_phone", sa.Text(), nullable=True),
        sa.Column("contact_email", postgresql.CITEXT(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
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
        sa.UniqueConstraint("agency_id", "user_id", name="uq_guardian_per_user_per_agency"),
    )
    op.create_index(
        "idx_guardian_profiles_agency_id",
        "guardian_profiles",
        ["agency_id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "idx_guardian_profiles_user_id",
        "guardian_profiles",
        ["user_id"],
    )
    op.execute(
        "CREATE TRIGGER trg_guardian_profiles_updated_at "
        "BEFORE UPDATE ON guardian_profiles "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    # ============================================================
    # patient_guardian_relationships
    # ============================================================
    # Many-to-many patient ↔ guardian. `relationship_type` says *how*
    # they're related (spouse, parent, conservator, etc.); `is_legal`
    # marks whether this relationship grants legal authority (only
    # legal relationships may e.g. sign off on a service verification).
    op.create_table(
        "patient_guardian_relationships",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "agency_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agencies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "patient_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("patient_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "guardian_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("guardian_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "relationship_type",
            postgresql.ENUM(name="relationship_type", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "is_legal",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        # Validity window — open-ended by default
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_until", sa.Date(), nullable=True),
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
        # One (patient, guardian, type) triple per agency
        sa.UniqueConstraint(
            "agency_id",
            "patient_id",
            "guardian_id",
            "relationship_type",
            name="uq_patient_guardian_rel",
        ),
        sa.CheckConstraint(
            "(valid_until IS NULL) OR (valid_from IS NULL) OR (valid_until >= valid_from)",
            name="ck_relationship_valid_dates",
        ),
    )
    op.create_index(
        "idx_pgr_patient_id",
        "patient_guardian_relationships",
        ["patient_id"],
    )
    op.create_index(
        "idx_pgr_guardian_id",
        "patient_guardian_relationships",
        ["guardian_id"],
    )
    op.create_index(
        "idx_pgr_agency_id",
        "patient_guardian_relationships",
        ["agency_id"],
    )
    op.execute(
        "CREATE TRIGGER trg_pgr_updated_at "
        "BEFORE UPDATE ON patient_guardian_relationships "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    # ============================================================
    # RLS
    # ============================================================
    for table in (
        "patient_profiles",
        "guardian_profiles",
        "patient_guardian_relationships",
    ):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    # ---- patient_profiles ----
    # Super Admin: all. AGENCY_ADMIN at the agency: all of that agency's
    # patients. The patient themselves: only their own row.
    op.execute(
        """
        CREATE POLICY patient_profiles_select ON patient_profiles
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
        CREATE POLICY patient_profiles_modify ON patient_profiles
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

    # ---- guardian_profiles ----
    op.execute(
        """
        CREATE POLICY guardian_profiles_select ON guardian_profiles
        FOR SELECT
        USING (
            app.is_super_admin()
            OR user_id = app.current_user_id()
            OR EXISTS (
                SELECT 1 FROM patient_guardian_relationships pgr
                JOIN patient_profiles pp ON pp.id = pgr.patient_id
                WHERE pgr.guardian_id = guardian_profiles.id
                  AND pp.user_id = app.current_user_id()
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
        CREATE POLICY guardian_profiles_modify ON guardian_profiles
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

    # ---- patient_guardian_relationships ----
    op.execute(
        """
        CREATE POLICY patient_guardian_relationships_select
        ON patient_guardian_relationships
        FOR SELECT
        USING (
            app.is_super_admin()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND agency_id = app.current_agency_id()
            )
            OR EXISTS (
                SELECT 1 FROM patient_profiles pp
                WHERE pp.id = patient_guardian_relationships.patient_id
                  AND pp.user_id = app.current_user_id()
            )
            OR EXISTS (
                SELECT 1 FROM guardian_profiles gp
                WHERE gp.id = patient_guardian_relationships.guardian_id
                  AND gp.user_id = app.current_user_id()
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY patient_guardian_relationships_modify
        ON patient_guardian_relationships
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


def downgrade() -> None:
    # Drop policies
    op.execute("DROP POLICY IF EXISTS patient_guardian_relationships_modify ON patient_guardian_relationships")
    op.execute("DROP POLICY IF EXISTS patient_guardian_relationships_select ON patient_guardian_relationships")
    op.execute("DROP POLICY IF EXISTS guardian_profiles_modify ON guardian_profiles")
    op.execute("DROP POLICY IF EXISTS guardian_profiles_select ON guardian_profiles")
    op.execute("DROP POLICY IF EXISTS patient_profiles_modify ON patient_profiles")
    op.execute("DROP POLICY IF EXISTS patient_profiles_select ON patient_profiles")

    # Drop triggers
    op.execute("DROP TRIGGER IF EXISTS trg_pgr_updated_at ON patient_guardian_relationships")
    op.execute("DROP TRIGGER IF EXISTS trg_guardian_profiles_updated_at ON guardian_profiles")
    op.execute("DROP TRIGGER IF EXISTS trg_patient_profiles_updated_at ON patient_profiles")

    # Disable RLS before dropping tables
    for table in (
        "patient_guardian_relationships",
        "guardian_profiles",
        "patient_profiles",
    ):
        op.execute(f"ALTER TABLE IF EXISTS {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE IF EXISTS {table} DISABLE ROW LEVEL SECURITY")

    op.drop_table("patient_guardian_relationships")
    op.drop_table("guardian_profiles")
    op.drop_table("patient_profiles")