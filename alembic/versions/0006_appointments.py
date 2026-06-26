"""Appointments + service items tables (schema doc §8).

Tables:
- appointments                  — scheduled visit linking a patient to a staff
                                  member at an agency, with start/end, status,
                                  and confirmation flow
- appointment_service_items     — line items: each service delivered during
                                  the appointment (PERSONAL_CARE, RESPITE, …)

Both tables are agency-scoped. RLS follows the established pattern:
SUPER_ADMIN full; AGENCY_ADMIN at the agency sees / manages everything;
STAFF sees appointments they're assigned to; PATIENT sees their own
appointments; GUARDIAN (linked to the patient via
patient_guardian_relationships) sees the patient's appointments.

Revision ID: 0006_appointments
Revises: 0005_patients_guardians
Create Date: 2026-06-27 18:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006_appointments"
down_revision: str | Sequence[str] | None = "0005_patients_guardians"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ============================================================
    # appointments
    # ============================================================
    op.create_table(
        "appointments",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "agency_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agencies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The care recipient — a row in `patient_profiles`
        sa.Column(
            "patient_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("patient_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The staff member assigned to perform the visit. Nullable until
        # an assignment is made (DRAFT / awaiting-assignment flow).
        sa.Column(
            "staff_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("staff_profiles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Which program the visit is billed under (PCA, CFSS, …).
        # Optional — agency may schedule cross-program visits.
        sa.Column(
            "program_type",
            postgresql.ENUM(name="program_type", create_type=False),
            nullable=True,
        ),
        # Window for the visit
        sa.Column("scheduled_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_end", sa.DateTime(timezone=True), nullable=False),
        # Status lifecycle (DRAFT → SCHEDULED → … → COMPLETED → PAID)
        sa.Column(
            "status",
            postgresql.ENUM(name="appointment_status", create_type=False),
            nullable=False,
            server_default="DRAFT",
        ),
        # Confirmation flow (patient / guardian acceptance)
        sa.Column(
            "confirmation_status",
            postgresql.ENUM(name="confirmation_status", create_type=False),
            nullable=True,
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmation_note", sa.Text(), nullable=True),
        # Visit / service-verification timestamps
        sa.Column("checked_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checked_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        # Location / context notes
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        # Cancellation reason (set when status = CANCELLED)
        sa.Column("cancelled_reason", sa.Text(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
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
            "scheduled_end > scheduled_start",
            name="ck_appointment_end_after_start",
        ),
        sa.CheckConstraint(
            "(checked_in_at IS NULL) OR (checked_out_at IS NULL) OR "
            "(checked_out_at >= checked_in_at)",
            name="ck_appointment_checkout_after_checkin",
        ),
    )
    op.create_index("idx_appointments_agency_id", "appointments", ["agency_id"])
    op.create_index("idx_appointments_patient_id", "appointments", ["patient_id"])
    op.create_index(
        "idx_appointments_staff_id",
        "appointments",
        ["staff_id"],
        postgresql_where=sa.text("staff_id IS NOT NULL"),
    )
    op.create_index(
        "idx_appointments_scheduled_start",
        "appointments",
        ["scheduled_start"],
    )
    op.create_index(
        "idx_appointments_status",
        "appointments",
        ["status"],
        postgresql_where=sa.text("status IN ('SCHEDULED', 'CONFIRMED', 'ASSIGNED')"),
    )
    op.execute(
        "CREATE TRIGGER trg_appointments_updated_at "
        "BEFORE UPDATE ON appointments "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    # ============================================================
    # appointment_service_items
    # ============================================================
    op.create_table(
        "appointment_service_items",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "appointment_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("appointments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agency_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agencies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "service_type",
            postgresql.ENUM(name="service_type", create_type=False),
            nullable=False,
        ),
        # Optional — minutes planned for this specific item
        sa.Column("planned_minutes", sa.Integer(), nullable=True),
        # Whether the item was actually delivered
        sa.Column(
            "status",
            postgresql.ENUM(name="service_item_status", create_type=False),
            nullable=False,
            server_default="PENDING",
        ),
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
        sa.CheckConstraint(
            "(planned_minutes IS NULL) OR (planned_minutes > 0)",
            name="ck_service_item_planned_minutes_positive",
        ),
    )
    op.create_index(
        "idx_service_items_appointment_id",
        "appointment_service_items",
        ["appointment_id"],
    )
    op.create_index(
        "idx_service_items_agency_id",
        "appointment_service_items",
        ["agency_id"],
    )
    op.execute(
        "CREATE TRIGGER trg_service_items_updated_at "
        "BEFORE UPDATE ON appointment_service_items "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    # ============================================================
    # RLS
    # ============================================================
    for table in ("appointments", "appointment_service_items"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    # ---- appointments ----
    # Super Admin: all. AGENCY_ADMIN at the agency: all of that agency's.
    # STAFF: appointments where they are the assignee (staff_id).
    # PATIENT: appointments where they're the patient.
    # GUARDIAN: appointments where the patient is linked to them via
    # patient_guardian_relationships.
    op.execute(
        """
        CREATE POLICY appointments_select ON appointments
        FOR SELECT
        USING (
            app.is_super_admin()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND agency_id = app.current_agency_id()
            )
            OR (
                staff_id IS NOT NULL
                AND EXISTS (
                    SELECT 1 FROM staff_profiles sp
                    WHERE sp.id = appointments.staff_id
                      AND sp.user_id = app.current_user_id()
                )
            )
            OR EXISTS (
                SELECT 1 FROM patient_profiles pp
                WHERE pp.id = appointments.patient_id
                  AND pp.user_id = app.current_user_id()
            )
            OR EXISTS (
                SELECT 1 FROM patient_guardian_relationships pgr
                JOIN guardian_profiles gp ON gp.id = pgr.guardian_id
                WHERE pgr.patient_id = appointments.patient_id
                  AND pgr.agency_id = appointments.agency_id
                  AND gp.user_id = app.current_user_id()
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY appointments_modify ON appointments
        FOR ALL
        USING (
            app.is_super_admin()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND agency_id = app.current_agency_id()
            )
            OR (
                staff_id IS NOT NULL
                AND EXISTS (
                    SELECT 1 FROM staff_profiles sp
                    WHERE sp.id = appointments.staff_id
                      AND sp.user_id = app.current_user_id()
                )
            )
        )
        WITH CHECK (
            app.is_super_admin()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND agency_id = app.current_agency_id()
            )
            OR (
                staff_id IS NOT NULL
                AND EXISTS (
                    SELECT 1 FROM staff_profiles sp
                    WHERE sp.id = appointments.staff_id
                      AND sp.user_id = app.current_user_id()
                )
            )
        )
        """
    )

    # ---- appointment_service_items ----
    # Inherits access from the parent appointment: any caller who can
    # SELECT / modify the appointment can SELECT / modify its items.
    op.execute(
        """
        CREATE POLICY appointment_service_items_select
        ON appointment_service_items
        FOR SELECT
        USING (
            app.is_super_admin()
            OR EXISTS (
                SELECT 1 FROM appointments a
                WHERE a.id = appointment_service_items.appointment_id
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY appointment_service_items_modify
        ON appointment_service_items
        FOR ALL
        USING (
            app.is_super_admin()
            OR EXISTS (
                SELECT 1 FROM appointments a
                WHERE a.id = appointment_service_items.appointment_id
                  AND (
                    (
                        app.has_agency_role('AGENCY_ADMIN')
                        AND a.agency_id = app.current_agency_id()
                    )
                    OR (
                        a.staff_id IS NOT NULL
                        AND EXISTS (
                            SELECT 1 FROM staff_profiles sp
                            WHERE sp.id = a.staff_id
                              AND sp.user_id = app.current_user_id()
                        )
                    )
                  )
            )
        )
        WITH CHECK (
            app.is_super_admin()
            OR EXISTS (
                SELECT 1 FROM appointments a
                WHERE a.id = appointment_service_items.appointment_id
                  AND (
                    (
                        app.has_agency_role('AGENCY_ADMIN')
                        AND a.agency_id = app.current_agency_id()
                    )
                    OR (
                        a.staff_id IS NOT NULL
                        AND EXISTS (
                            SELECT 1 FROM staff_profiles sp
                            WHERE sp.id = a.staff_id
                              AND sp.user_id = app.current_user_id()
                        )
                    )
                  )
            )
        )
        """
    )


def downgrade() -> None:
    # Drop policies
    op.execute("DROP POLICY IF EXISTS appointment_service_items_modify ON appointment_service_items")
    op.execute("DROP POLICY IF EXISTS appointment_service_items_select ON appointment_service_items")
    op.execute("DROP POLICY IF EXISTS appointments_modify ON appointments")
    op.execute("DROP POLICY IF EXISTS appointments_select ON appointments")

    # Drop triggers
    op.execute("DROP TRIGGER IF EXISTS trg_service_items_updated_at ON appointment_service_items")
    op.execute("DROP TRIGGER IF EXISTS trg_appointments_updated_at ON appointments")

    # Disable RLS before dropping tables
    for table in ("appointment_service_items", "appointments"):
        op.execute(f"ALTER TABLE IF EXISTS {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE IF EXISTS {table} DISABLE ROW LEVEL SECURITY")

    op.drop_table("appointment_service_items")
    op.drop_table("appointments")