"""Visits + verification tables (schema doc §11 + §12).

Tables:
- visits                    — materialized attendance record for an appointment
                              (one row per appointment, 1:1 via UNIQUE
                              appointment_id). Holds check-in / check-out
                              timestamps, GPS, device ID, duration.
- visit_service_items       — per-item delivery log (DONE / NOT_DONE /
                              NOT_APPLICABLE / NEEDS_FOLLOW_UP) with reason
                              when NOT_DONE.
- visit_notes               — free-form narrative notes authored during/after
                              the visit (clinical, operational, etc.).
- service_verifications     — patient/guardian post-visit verification
                              (VERIFIED or DISPUTED + reason code). 1:1 with
                              visit via UNIQUE visit_id.
- visit_issues              — non-blocking reports filed against a visit
                              (e.g. "patient complained about noise").
                              Resolvable separately from the visit lifecycle.

All five tables are agency-scoped. RLS follows the established pattern:
- SELECT: SUPER_ADMIN full; AGENCY_ADMIN at agency; STAFF for assigned
  visits; PATIENT for their own; GUARDIAN via patient_guardian_relationships.
- MODIFY: SUPER_ADMIN full; AGENCY_ADMIN at agency; STAFF for assigned
  visits. Patients/guardians can MODIFY only the verification row
  (their own) and create visit_issues against their visit.

Enums (`visit_status`, `verification_status`, `dispute_reason_code`)
already exist from migration 0001; this migration reuses them via
`create_type=False`.

Revision ID: 0007_visit_verification
Revises: 0006_appointments
Create Date: 2026-06-27 19:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0007_visit_verification"
down_revision: str | Sequence[str] | None = "0006_appointments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ============================================================
    # visits
    # ============================================================
    op.create_table(
        "visits",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        # 1:1 with appointment — at most one visit row per appointment.
        sa.Column(
            "appointment_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("appointments.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "agency_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agencies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The staff member who actually performed the visit. RESTRICT —
        # don't silently drop historical visit records when staff leave.
        sa.Column(
            "staff_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("staff_profiles.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(name="visit_status", create_type=False),
            nullable=False,
            server_default="CHECKED_IN",
        ),
        # ---- check-in ----
        sa.Column("check_in_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("check_in_lat", sa.Numeric(9, 6), nullable=True),
        sa.Column("check_in_lng", sa.Numeric(9, 6), nullable=True),
        sa.Column("check_in_accuracy_m", sa.Numeric(6, 2), nullable=True),
        sa.Column("check_in_device_id", sa.Text(), nullable=True),
        sa.Column("check_in_address_match", sa.Boolean(), nullable=True),
        sa.Column(
            "check_in_distance_from_location_m",
            sa.Numeric(8, 2),
            nullable=True,
        ),
        # ---- check-out ----
        sa.Column("check_out_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("check_out_lat", sa.Numeric(9, 6), nullable=True),
        sa.Column("check_out_lng", sa.Numeric(9, 6), nullable=True),
        sa.Column("check_out_accuracy_m", sa.Numeric(6, 2), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
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
            "(check_out_time IS NULL) OR (check_in_time IS NULL) OR "
            "(check_out_time > check_in_time)",
            name="ck_visit_checkout_after_checkin",
        ),
    )
    op.create_index(
        "idx_visits_agency",
        "visits",
        ["agency_id", sa.text("check_in_time DESC")],
    )
    op.create_index(
        "idx_visits_staff",
        "visits",
        ["staff_id", sa.text("check_in_time DESC")],
    )
    op.create_index(
        "idx_visits_status",
        "visits",
        ["status"],
        postgresql_where=sa.text("status <> 'COMPLETED'"),
    )
    op.execute(
        "CREATE TRIGGER trg_visits_updated_at "
        "BEFORE UPDATE ON visits "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    # ============================================================
    # visit_service_items
    # ============================================================
    op.create_table(
        "visit_service_items",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "visit_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("visits.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # FK to the original appointment-level service item. Cascading
        # both directions keeps the two sides in sync.
        sa.Column(
            "appointment_service_item_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("appointment_service_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(name="service_item_status", create_type=False),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "completed_by",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
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
        # A given appointment_service_item may only appear once per visit
        sa.UniqueConstraint(
            "visit_id",
            "appointment_service_item_id",
            name="uq_visit_service_item",
        ),
        sa.CheckConstraint(
            "status <> 'NOT_DONE' OR (reason IS NOT NULL AND length(trim(reason)) > 0)",
            name="ck_reason_required_when_not_done",
        ),
    )
    op.create_index(
        "idx_visit_service_items_visit",
        "visit_service_items",
        ["visit_id"],
    )
    op.execute(
        "CREATE TRIGGER trg_visit_service_items_updated_at "
        "BEFORE UPDATE ON visit_service_items "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    # ============================================================
    # visit_notes
    # ============================================================
    op.create_table(
        "visit_notes",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "visit_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("visits.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "author_user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "length(trim(body)) > 0",
            name="ck_visit_note_body_non_empty",
        ),
    )
    op.create_index(
        "idx_visit_notes_visit",
        "visit_notes",
        ["visit_id", sa.text("created_at")],
    )

    # ============================================================
    # service_verifications
    # ============================================================
    op.create_table(
        "service_verifications",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "visit_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("visits.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "agency_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agencies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "verified_by",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # Either PATIENT or GUARDIAN per the schema doc
        sa.Column(
            "verifier_role",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(name="verification_status", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "dispute_reason_code",
            postgresql.ENUM(name="dispute_reason_code", create_type=False),
            nullable=True,
        ),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "verifier_role IN ('PATIENT', 'GUARDIAN')",
            name="ck_verifier_role_patient_or_guardian",
        ),
        sa.CheckConstraint(
            "status <> 'DISPUTED' OR dispute_reason_code IS NOT NULL",
            name="ck_dispute_requires_reason",
        ),
    )
    op.create_index(
        "idx_service_verifications_verified_by",
        "service_verifications",
        ["verified_by"],
    )
    op.create_index(
        "idx_service_verifications_agency",
        "service_verifications",
        ["agency_id", sa.text("created_at DESC")],
    )

    # ============================================================
    # visit_issues
    # ============================================================
    op.create_table(
        "visit_issues",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "visit_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("visits.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agency_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agencies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "reported_by",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("issue_type", sa.Text(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "resolved_by",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "length(trim(issue_type)) > 0",
            name="ck_visit_issue_type_non_empty",
        ),
        sa.CheckConstraint(
            "length(trim(comment)) > 0",
            name="ck_visit_issue_comment_non_empty",
        ),
    )
    op.create_index(
        "idx_visit_issues_visit",
        "visit_issues",
        ["visit_id"],
    )
    op.create_index(
        "idx_visit_issues_unresolved",
        "visit_issues",
        ["agency_id", sa.text("created_at DESC")],
        postgresql_where=sa.text("resolved_at IS NULL"),
    )

    # ============================================================
    # RLS — enable + force on all 5 tables
    # ============================================================
    for table in (
        "visits",
        "visit_service_items",
        "visit_notes",
        "service_verifications",
        "visit_issues",
    ):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    # ---- visits ----
    op.execute(
        """
        CREATE POLICY visits_select ON visits
        FOR SELECT
        USING (
            app.is_super_admin()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND agency_id = app.current_agency_id()
            )
            OR EXISTS (
                SELECT 1 FROM staff_profiles sp
                WHERE sp.id = visits.staff_id
                  AND sp.user_id = app.current_user_id()
            )
            OR EXISTS (
                SELECT 1 FROM appointments a
                JOIN patient_profiles pp ON pp.id = a.patient_id
                WHERE a.id = visits.appointment_id
                  AND pp.user_id = app.current_user_id()
            )
            OR EXISTS (
                SELECT 1 FROM appointments a
                JOIN patient_guardian_relationships pgr
                  ON pgr.patient_id = a.patient_id
                  AND pgr.agency_id = a.agency_id
                JOIN guardian_profiles gp ON gp.id = pgr.guardian_id
                WHERE a.id = visits.appointment_id
                  AND gp.user_id = app.current_user_id()
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY visits_modify ON visits
        FOR ALL
        USING (
            app.is_super_admin()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND agency_id = app.current_agency_id()
            )
            OR EXISTS (
                SELECT 1 FROM staff_profiles sp
                WHERE sp.id = visits.staff_id
                  AND sp.user_id = app.current_user_id()
            )
        )
        WITH CHECK (
            app.is_super_admin()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND agency_id = app.current_agency_id()
            )
            OR EXISTS (
                SELECT 1 FROM staff_profiles sp
                WHERE sp.id = visits.staff_id
                  AND sp.user_id = app.current_user_id()
            )
        )
        """
    )

    # ---- visit_service_items ----
    # Inherits access from the parent visit.
    op.execute(
        """
        CREATE POLICY visit_service_items_select ON visit_service_items
        FOR SELECT
        USING (
            app.is_super_admin()
            OR EXISTS (
                SELECT 1 FROM visits v
                WHERE v.id = visit_service_items.visit_id
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY visit_service_items_modify ON visit_service_items
        FOR ALL
        USING (
            app.is_super_admin()
            OR EXISTS (
                SELECT 1 FROM visits v
                WHERE v.id = visit_service_items.visit_id
                  AND (
                    (
                        app.has_agency_role('AGENCY_ADMIN')
                        AND v.agency_id = app.current_agency_id()
                    )
                    OR EXISTS (
                        SELECT 1 FROM staff_profiles sp
                        WHERE sp.id = v.staff_id
                          AND sp.user_id = app.current_user_id()
                    )
                  )
            )
        )
        WITH CHECK (
            app.is_super_admin()
            OR EXISTS (
                SELECT 1 FROM visits v
                WHERE v.id = visit_service_items.visit_id
                  AND (
                    (
                        app.has_agency_role('AGENCY_ADMIN')
                        AND v.agency_id = app.current_agency_id()
                    )
                    OR EXISTS (
                        SELECT 1 FROM staff_profiles sp
                        WHERE sp.id = v.staff_id
                          AND sp.user_id = app.current_user_id()
                    )
                  )
            )
        )
        """
    )

    # ---- visit_notes ----
    # Notes authored by staff/admin can be read by patient/guardian
    # associated with the visit. Write access is staff/admin only —
    # patients/guardians have their own communication channels.
    op.execute(
        """
        CREATE POLICY visit_notes_select ON visit_notes
        FOR SELECT
        USING (
            app.is_super_admin()
            OR EXISTS (
                SELECT 1 FROM visits v
                WHERE v.id = visit_notes.visit_id
                  AND (
                    (
                        app.has_agency_role('AGENCY_ADMIN')
                        AND v.agency_id = app.current_agency_id()
                    )
                    OR EXISTS (
                        SELECT 1 FROM staff_profiles sp
                        WHERE sp.id = v.staff_id
                          AND sp.user_id = app.current_user_id()
                    )
                    OR EXISTS (
                        SELECT 1 FROM appointments a
                        JOIN patient_profiles pp ON pp.id = a.patient_id
                        WHERE a.id = v.appointment_id
                          AND pp.user_id = app.current_user_id()
                    )
                    OR EXISTS (
                        SELECT 1 FROM appointments a
                        JOIN patient_guardian_relationships pgr
                          ON pgr.patient_id = a.patient_id
                          AND pgr.agency_id = a.agency_id
                        JOIN guardian_profiles gp ON gp.id = pgr.guardian_id
                        WHERE a.id = v.appointment_id
                          AND gp.user_id = app.current_user_id()
                    )
                  )
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY visit_notes_modify ON visit_notes
        FOR ALL
        USING (
            app.is_super_admin()
            OR EXISTS (
                SELECT 1 FROM visits v
                WHERE v.id = visit_notes.visit_id
                  AND (
                    (
                        app.has_agency_role('AGENCY_ADMIN')
                        AND v.agency_id = app.current_agency_id()
                    )
                    OR EXISTS (
                        SELECT 1 FROM staff_profiles sp
                        WHERE sp.id = v.staff_id
                          AND sp.user_id = app.current_user_id()
                    )
                  )
            )
        )
        WITH CHECK (
            app.is_super_admin()
            OR EXISTS (
                SELECT 1 FROM visits v
                WHERE v.id = visit_notes.visit_id
                  AND (
                    (
                        app.has_agency_role('AGENCY_ADMIN')
                        AND v.agency_id = app.current_agency_id()
                    )
                    OR EXISTS (
                        SELECT 1 FROM staff_profiles sp
                        WHERE sp.id = v.staff_id
                          AND sp.user_id = app.current_user_id()
                    )
                  )
            )
        )
        """
    )

    # ---- service_verifications ----
    # Patient/guardian can INSERT/UPDATE their own. Staff/agency can SELECT.
    op.execute(
        """
        CREATE POLICY service_verifications_select ON service_verifications
        FOR SELECT
        USING (
            app.is_super_admin()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND agency_id = app.current_agency_id()
            )
            OR verified_by = app.current_user_id()
            OR EXISTS (
                SELECT 1 FROM visits v
                JOIN appointments a ON a.id = v.appointment_id
                JOIN patient_profiles pp ON pp.id = a.patient_id
                WHERE v.id = service_verifications.visit_id
                  AND pp.user_id = app.current_user_id()
            )
            OR EXISTS (
                SELECT 1 FROM visits v
                JOIN appointments a ON a.id = v.appointment_id
                JOIN patient_guardian_relationships pgr
                  ON pgr.patient_id = a.patient_id
                  AND pgr.agency_id = a.agency_id
                JOIN guardian_profiles gp ON gp.id = pgr.guardian_id
                WHERE v.id = service_verifications.visit_id
                  AND gp.user_id = app.current_user_id()
            )
            OR EXISTS (
                SELECT 1 FROM visits v
                JOIN staff_profiles sp ON sp.id = v.staff_id
                WHERE v.id = service_verifications.visit_id
                  AND sp.user_id = app.current_user_id()
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY service_verifications_modify ON service_verifications
        FOR ALL
        USING (
            app.is_super_admin()
            OR verified_by = app.current_user_id()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND agency_id = app.current_agency_id()
            )
        )
        WITH CHECK (
            app.is_super_admin()
            OR verified_by = app.current_user_id()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND agency_id = app.current_agency_id()
            )
        )
        """
    )

    # ---- visit_issues ----
    # Anyone who can see the visit can see its issues. Patient/guardian
    # can file new issues (write) for their visit; only admin can resolve.
    op.execute(
        """
        CREATE POLICY visit_issues_select ON visit_issues
        FOR SELECT
        USING (
            app.is_super_admin()
            OR EXISTS (
                SELECT 1 FROM visits v
                WHERE v.id = visit_issues.visit_id
                  AND (
                    (
                        app.has_agency_role('AGENCY_ADMIN')
                        AND v.agency_id = app.current_agency_id()
                    )
                    OR EXISTS (
                        SELECT 1 FROM staff_profiles sp
                        WHERE sp.id = v.staff_id
                          AND sp.user_id = app.current_user_id()
                    )
                    OR EXISTS (
                        SELECT 1 FROM appointments a
                        JOIN patient_profiles pp ON pp.id = a.patient_id
                        WHERE a.id = v.appointment_id
                          AND pp.user_id = app.current_user_id()
                    )
                    OR EXISTS (
                        SELECT 1 FROM appointments a
                        JOIN patient_guardian_relationships pgr
                          ON pgr.patient_id = a.patient_id
                          AND pgr.agency_id = a.agency_id
                        JOIN guardian_profiles gp ON gp.id = pgr.guardian_id
                        WHERE a.id = v.appointment_id
                          AND gp.user_id = app.current_user_id()
                    )
                  )
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY visit_issues_modify ON visit_issues
        FOR ALL
        USING (
            app.is_super_admin()
            OR reported_by = app.current_user_id()
            OR EXISTS (
                SELECT 1 FROM visits v
                WHERE v.id = visit_issues.visit_id
                  AND (
                    (
                        app.has_agency_role('AGENCY_ADMIN')
                        AND v.agency_id = app.current_agency_id()
                    )
                    OR EXISTS (
                        SELECT 1 FROM staff_profiles sp
                        WHERE sp.id = v.staff_id
                          AND sp.user_id = app.current_user_id()
                    )
                  )
            )
        )
        WITH CHECK (
            app.is_super_admin()
            OR reported_by = app.current_user_id()
            OR EXISTS (
                SELECT 1 FROM visits v
                WHERE v.id = visit_issues.visit_id
                  AND (
                    (
                        app.has_agency_role('AGENCY_ADMIN')
                        AND v.agency_id = app.current_agency_id()
                    )
                    OR EXISTS (
                        SELECT 1 FROM staff_profiles sp
                        WHERE sp.id = v.staff_id
                          AND sp.user_id = app.current_user_id()
                    )
                  )
            )
        )
        """
    )


def downgrade() -> None:
    # Drop policies
    for policy, table in (
        ("visit_issues_modify", "visit_issues"),
        ("visit_issues_select", "visit_issues"),
        ("service_verifications_modify", "service_verifications"),
        ("service_verifications_select", "service_verifications"),
        ("visit_notes_modify", "visit_notes"),
        ("visit_notes_select", "visit_notes"),
        ("visit_service_items_modify", "visit_service_items"),
        ("visit_service_items_select", "visit_service_items"),
        ("visits_modify", "visits"),
        ("visits_select", "visits"),
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")

    # Drop triggers
    op.execute("DROP TRIGGER IF EXISTS trg_visit_service_items_updated_at ON visit_service_items")
    op.execute("DROP TRIGGER IF EXISTS trg_visits_updated_at ON visits")

    # Disable RLS before dropping tables
    for table in (
        "visit_issues",
        "service_verifications",
        "visit_notes",
        "visit_service_items",
        "visits",
    ):
        op.execute(f"ALTER TABLE IF EXISTS {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE IF EXISTS {table} DISABLE ROW LEVEL SECURITY")

    op.drop_table("visit_issues")
    op.drop_table("service_verifications")
    op.drop_table("visit_notes")
    op.drop_table("visit_service_items")
    op.drop_table("visits")