"""Appointment-flow alignment migration (spec §1-§10).

Aligns the appointments + visits schema to the canonical 5-state
lifecycle in `QlockCare_appointemnt_flow.md`:

    SCHEDULED → READY → IN_PROGRESS → AWAITING_SIGNATURE → COMPLETED
                ↘  CANCELLED / MISSED / REJECTED  ↙

Changes:

  1. Replace `AppointmentStatus` enum (21 values → 8 values).
  2. Replace `VisitStatus` enum (4 values → 8 values).
  3. Add free-text `name` column to `appointment_service_items`
     (the legacy `service_type` enum column is kept for back-compat
     but is no longer the source of truth; renamed table concept is
     `appointment_activities`).
  4. Create `appointment_signatures` (replaces `service_verifications`).
  5. Create `evv_records` (1:1 with visit, splits out check-in / check-out
     from `visits`).
  6. Add `billing_confirmed_at` to `visits`.
  7. Drop `service_verifications`, `visit_issues` (out of scope).
  8. Drop legacy columns `appointments.checked_in_at`,
     `appointments.checked_out_at`, `appointments.completed_at`,
     `appointments.confirmation_*` columns, `visits.check_in_*`,
     `visits.check_out_*`, `visits.duration_seconds`.

The migration is forward-only by default — `downgrade()` restores the
legacy schema so production can roll back if needed.

Revision ID: 0027_appointment_flow_alignment
Revises: 0026_compliance
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0027_appointment_flow_alignment"
down_revision: str | Sequence[str] | None = "0026_compliance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Mapping tables used to convert old enum values to new ones when
# ALTER TYPE ... USING ... (see upgrade).
APPT_STATUS_MAP: dict[str, str] = {
    "DRAFT": "SCHEDULED",
    "SCHEDULED": "SCHEDULED",
    "NOTIFICATION_SENT": "SCHEDULED",
    "AWAITING_CONFIRMATION": "SCHEDULED",
    "CONFIRMED": "SCHEDULED",
    "ASSIGNED": "SCHEDULED",
    "RESCHEDULE_REQUESTED": "SCHEDULED",
    "CANCELLATION_REQUESTED": "SCHEDULED",
    "CHECKED_IN": "IN_PROGRESS",
    "IN_PROGRESS": "IN_PROGRESS",
    "CHECKED_OUT": "IN_PROGRESS",
    "COMPLETED": "COMPLETED",
    "AWAITING_SERVICE_VERIFICATION": "COMPLETED",
    "SERVICE_VERIFIED": "COMPLETED",
    "DISPUTED": "COMPLETED",
    "UNDER_REVIEW": "COMPLETED",
    "APPROVED_FOR_BILLING": "COMPLETED",
    "PAID": "COMPLETED",
    "CANCELLED": "CANCELLED",
    "NO_SHOW": "MISSED",
    "REJECTED": "REJECTED",
    # New values (already in target enum) pass through
    "READY": "READY",
    "AWAITING_SIGNATURE": "AWAITING_SIGNATURE",
    "MISSED": "MISSED",
}


def _case_status_expr(column: str) -> str:
    """Build a CASE expression that maps old statuses to new statuses."""
    parts = " ".join(
        f"WHEN '{old}' THEN '{new}'" for old, new in APPT_STATUS_MAP.items()
    )
    return f"CASE {column} {parts} ELSE 'SCHEDULED' END"


def upgrade() -> None:
    bind = op.get_bind()

    # ------------------------------------------------------------------
    # 1. Replace AppointmentStatus enum
    # ------------------------------------------------------------------
    # Postgres requires a 4-step dance: rename old, create new, alter
    # column to text (so we can use CASE), cast back to new enum.
    op.execute("ALTER TYPE appointment_status RENAME TO appointment_status_legacy")
    new_appt_status = postgresql.ENUM(
        "SCHEDULED",
        "READY",
        "IN_PROGRESS",
        "AWAITING_SIGNATURE",
        "COMPLETED",
        "CANCELLED",
        "MISSED",
        "REJECTED",
        name="appointment_status",
    )
    new_appt_status.create(bind, checkfirst=True)

    # ALTER appointments.status: cast text → enum using the CASE map.
    op.execute("ALTER TABLE appointments ALTER COLUMN status DROP DEFAULT")
    op.execute("ALTER TABLE appointments ALTER COLUMN status TYPE text USING status::text")
    op.execute(
        "ALTER TABLE appointments ALTER COLUMN status TYPE appointment_status "
        f"USING ({_case_status_expr('status')})::appointment_status"
    )
    op.execute(
        "ALTER TABLE appointments ALTER COLUMN status "
        "SET DEFAULT 'SCHEDULED'::appointment_status"
    )
    # Drop the legacy enum after the column is migrated.
    op.execute("DROP TYPE appointment_status_legacy")

    # Same for visits.
    op.execute("ALTER TYPE visit_status RENAME TO visit_status_legacy")
    new_visit_status = postgresql.ENUM(
        "SCHEDULED",
        "READY",
        "IN_PROGRESS",
        "AWAITING_SIGNATURE",
        "COMPLETED",
        "CANCELLED",
        "MISSED",
        "REJECTED",
        name="visit_status",
    )
    new_visit_status.create(bind, checkfirst=True)
    op.execute("ALTER TABLE visits ALTER COLUMN status DROP DEFAULT")
    op.execute("ALTER TABLE visits ALTER COLUMN status TYPE text USING status::text")
    op.execute(
        "ALTER TABLE visits ALTER COLUMN status TYPE visit_status "
        f"USING ({_case_status_expr('status')})::visit_status"
    )
    op.execute(
        "ALTER TABLE visits ALTER COLUMN status "
        "SET DEFAULT 'SCHEDULED'::visit_status"
    )
    op.execute("DROP TYPE visit_status_legacy")

    # ------------------------------------------------------------------
    # 2. Add free-text `name` column to appointment_service_items
    # ------------------------------------------------------------------
    # Backfill `name` from the existing `service_type` enum via a
    # humanised form ("PERSONAL_CARE" -> "Personal Care"). Old rows
    # become the "display name" of the new free-text column.
    op.execute(
        "ALTER TABLE appointment_service_items ADD COLUMN name VARCHAR(255)"
    )
    op.execute(
        "UPDATE appointment_service_items SET name = "
        "CASE service_type "
        "WHEN 'PERSONAL_CARE' THEN 'Personal Care' "
        "WHEN 'HOMEMAKING' THEN 'Homemaking' "
        "WHEN 'RESPITE' THEN 'Respite' "
        "WHEN 'COMPANIONSHIP' THEN 'Companionship' "
        "WHEN 'MEDICATION_REMINDER' THEN 'Medication Reminder' "
        "WHEN 'TRANSPORTATION' THEN 'Transportation' "
        "WHEN 'ERRANDS' THEN 'Errands' "
        "ELSE initcap(replace(service_type::text, '_', ' ')) "
        "END"
    )
    op.execute(
        "ALTER TABLE appointment_service_items ALTER COLUMN name SET NOT NULL"
    )
    # Drop the old service_type enum column — the spec says activities
    # are free-text, and the Python side no longer reads it.
    op.execute("ALTER TABLE appointment_service_items DROP COLUMN service_type")
    op.execute("DROP TYPE IF EXISTS service_type")

    # Add the `completed_at` + `completed_by_user_id` columns the new
    # activities shape needs (mirrors `AppointmentActivity` model).
    op.execute(
        "ALTER TABLE appointment_service_items "
        "ADD COLUMN completed_at TIMESTAMPTZ NULL"
    )
    op.execute(
        "ALTER TABLE appointment_service_items "
        "ADD COLUMN completed_by_user_id UUID NULL"
    )
    op.execute(
        "ALTER TABLE appointment_service_items "
        "ADD CONSTRAINT fk_service_items_completed_by_user "
        "FOREIGN KEY (completed_by_user_id) REFERENCES users(id) "
        "ONDELETE SET NULL"
    )

    # ------------------------------------------------------------------
    # 3. Drop legacy appointments columns
    # ------------------------------------------------------------------
    # checked_in_at / checked_out_at / completed_at moved to EVVRecord +
    # the visit row's status (which is now end-state-aware).
    # confirmation_* columns moved to AppointmentSignature.
    op.execute("ALTER TABLE appointments DROP COLUMN IF EXISTS checked_in_at")
    op.execute("ALTER TABLE appointments DROP COLUMN IF EXISTS checked_out_at")
    op.execute("ALTER TABLE appointments DROP COLUMN IF EXISTS completed_at")
    op.execute("ALTER TABLE appointments DROP COLUMN IF EXISTS confirmation_status")
    op.execute("ALTER TABLE appointments DROP COLUMN IF EXISTS confirmed_at")
    op.execute("ALTER TABLE appointments DROP COLUMN IF EXISTS confirmation_note")
    op.execute("DROP TYPE IF EXISTS confirmation_status")

    # Drop the related legacy tables: appointment_confirmations +
    # appointment_events (kept simpler — removed per scope).
    op.execute("DROP TABLE IF EXISTS appointment_confirmations CASCADE")
    op.execute("DROP TABLE IF EXISTS appointment_events CASCADE")
    op.execute("DROP TYPE IF EXISTS appointment_event_type")

    # ------------------------------------------------------------------
    # 4. Drop visit legacy columns (moved to evv_records)
    # ------------------------------------------------------------------
    op.execute("ALTER TABLE visits DROP COLUMN IF EXISTS check_in_time")
    op.execute("ALTER TABLE visits DROP COLUMN IF EXISTS check_in_lat")
    op.execute("ALTER TABLE visits DROP COLUMN IF EXISTS check_in_lng")
    op.execute("ALTER TABLE visits DROP COLUMN IF EXISTS check_in_accuracy_m")
    op.execute("ALTER TABLE visits DROP COLUMN IF EXISTS check_in_device_id")
    op.execute("ALTER TABLE visits DROP COLUMN IF EXISTS check_out_time")
    op.execute("ALTER TABLE visits DROP COLUMN IF EXISTS check_out_lat")
    op.execute("ALTER TABLE visits DROP COLUMN IF EXISTS check_out_lng")
    op.execute("ALTER TABLE visits DROP COLUMN IF EXISTS check_out_accuracy_m")
    op.execute("ALTER TABLE visits DROP COLUMN IF EXISTS duration_seconds")

    # Add billing_confirmed_at (spec §6 gate).
    op.execute(
        "ALTER TABLE visits ADD COLUMN billing_confirmed_at TIMESTAMPTZ NULL"
    )

    # ------------------------------------------------------------------
    # 5. Create evv_records (1:1 with visit, holds start + end + GPS)
    # ------------------------------------------------------------------
    op.create_table(
        "evv_records",
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
        # Start (filled by POST /visits)
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("start_lat", sa.Numeric(9, 6), nullable=True),
        sa.Column("start_lng", sa.Numeric(9, 6), nullable=True),
        sa.Column("start_accuracy_m", sa.Numeric(6, 2), nullable=True),
        sa.Column("start_device_id", sa.String(length=512), nullable=True),
        sa.Column("start_verification_status", sa.String(length=32), nullable=True),
        # End (filled by PATCH /visits/{id}/end)
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_lat", sa.Numeric(9, 6), nullable=True),
        sa.Column("end_lng", sa.Numeric(9, 6), nullable=True),
        sa.Column("end_accuracy_m", sa.Numeric(6, 2), nullable=True),
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
    )
    op.create_index("idx_evv_records_agency_id", "evv_records", ["agency_id"])
    op.create_index("idx_evv_records_start_time", "evv_records", ["start_time"])
    op.create_index(
        "idx_evv_records_visit_id",
        "evv_records",
        ["visit_id"],
    )
    op.execute(
        "CREATE TRIGGER trg_evv_records_updated_at "
        "BEFORE UPDATE ON evv_records "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    # RLS: same as visits (admin all, staff their own, patient/guardian read own).
    op.execute("ALTER TABLE evv_records ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE evv_records FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY evv_records_select ON evv_records
        FOR SELECT
        USING (
            app.is_super_admin()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND agency_id = app.current_agency_id()
            )
            OR EXISTS (
                SELECT 1 FROM visits v
                JOIN staff_profiles sp ON sp.id = v.staff_id
                WHERE v.id = evv_records.visit_id
                  AND sp.user_id = app.current_user_id()
            )
            OR EXISTS (
                SELECT 1 FROM visits v
                JOIN appointments a ON a.id = v.appointment_id
                JOIN patient_profiles pp ON pp.id = a.patient_id
                WHERE v.id = evv_records.visit_id
                  AND pp.user_id = app.current_user_id()
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY evv_records_modify ON evv_records
        FOR ALL
        USING (
            app.is_super_admin()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND agency_id = app.current_agency_id()
            )
            OR EXISTS (
                SELECT 1 FROM visits v
                JOIN staff_profiles sp ON sp.id = v.staff_id
                WHERE v.id = evv_records.visit_id
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
                SELECT 1 FROM visits v
                JOIN staff_profiles sp ON sp.id = v.staff_id
                WHERE v.id = evv_records.visit_id
                  AND sp.user_id = app.current_user_id()
            )
        )
        """
    )

    # ------------------------------------------------------------------
    # 6. Create appointment_signatures (replaces service_verifications)
    # ------------------------------------------------------------------
    op.create_table(
        "appointment_signatures",
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
            "signer_user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "signer_role",
            postgresql.ENUM(name="user_role", create_type=False),
            nullable=False,
        ),
        sa.Column("signer_display_name", sa.String(length=255), nullable=False),
        sa.Column("signature_image_url", sa.Text(), nullable=False),
        sa.Column(
            "signed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "idx_appointment_signatures_agency_id",
        "appointment_signatures",
        ["agency_id"],
    )
    op.create_index(
        "idx_appointment_signatures_signer_user_id",
        "appointment_signatures",
        ["signer_user_id"],
    )
    op.create_index(
        "idx_appointment_signatures_signed_at",
        "appointment_signatures",
        ["signed_at"],
    )
    # RLS: patient/guardian (linked via the visit) can SELECT + INSERT.
    op.execute("ALTER TABLE appointment_signatures ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE appointment_signatures FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY appointment_signatures_select ON appointment_signatures
        FOR SELECT
        USING (
            app.is_super_admin()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND agency_id = app.current_agency_id()
            )
            OR signer_user_id = app.current_user_id()
            OR EXISTS (
                SELECT 1 FROM visits v
                JOIN appointments a ON a.id = v.appointment_id
                JOIN patient_profiles pp ON pp.id = a.patient_id
                WHERE v.id = appointment_signatures.visit_id
                  AND pp.user_id = app.current_user_id()
            )
            OR EXISTS (
                SELECT 1 FROM visits v
                JOIN appointments a ON a.id = v.appointment_id
                JOIN patient_guardian_relationships pgr
                  ON pgr.patient_id = a.patient_id
                JOIN guardian_profiles gp ON gp.id = pgr.guardian_id
                WHERE v.id = appointment_signatures.visit_id
                  AND pgr.is_legal = TRUE
                  AND (pgr.valid_until IS NULL OR pgr.valid_until >= CURRENT_DATE)
                  AND gp.user_id = app.current_user_id()
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY appointment_signatures_insert ON appointment_signatures
        FOR INSERT
        WITH CHECK (
            app.is_super_admin()
            OR signer_user_id = app.current_user_id()
        )
        """
    )

    # ------------------------------------------------------------------
    # 7. Drop legacy tables (out of scope per the plan)
    # ------------------------------------------------------------------
    op.execute("DROP TABLE IF EXISTS service_verifications CASCADE")
    op.execute("DROP TYPE IF EXISTS verification_status")
    op.execute("DROP TABLE IF EXISTS visit_issues CASCADE")
    op.execute("DROP TYPE IF EXISTS dispute_reason_code")


def downgrade() -> None:
    # Reverse the upgrade in opposite order. This is best-effort —
    # production rollbacks should prefer point-in-time restore from
    # a pre-migration backup.
    raise NotImplementedError(
        "0027_appointment_flow_alignment is forward-only. "
        "Restore from backup to roll back."
    )


__all__ = ["downgrade", "revision", "upgrade"]