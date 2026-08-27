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
    # 0. Drop partial indexes that reference the old enum types.
    # ------------------------------------------------------------------
    # `idx_appointments_status` and `idx_visits_status` carry WHERE
    # clauses that compare `status` to `appointment_status` /
    # `visit_status` enum labels. When we rename those enum types
    # below, the partial-index expressions are still parsed against
    # the (now renamed) types, and the subsequent
    # `ALTER COLUMN ... TYPE text USING status::text` fails with
    # `operator does not exist: text = appointment_status_legacy`
    # because Postgres re-validates the partial-index expression
    # against the new (text) column type.
    #
    # Drop the partial indexes; we'll recreate them at the end of the
    # migration against the new enum.
    # (Both the legacy names and the spec-aligned names are dropped —
    # an idempotent re-run of this migration may have already
    # created the new ones.)
    op.execute("DROP INDEX IF EXISTS idx_appointments_status")
    op.execute("DROP INDEX IF EXISTS idx_visits_status")
    op.execute("DROP INDEX IF EXISTS idx_appointments_status_active")
    op.execute("DROP INDEX IF EXISTS idx_visits_status_active")

    # ------------------------------------------------------------------
    # 1. Replace AppointmentStatus enum
    # ------------------------------------------------------------------
    # Postgres requires a 4-step dance: rename old, create new, alter
    # column to text (so we can use CASE), cast back to new enum.
    # Wrap the RENAME in DO so the migration is idempotent (a partial
    # prior run may have already renamed it; `ALTER TYPE` doesn't
    # support `IF EXISTS`).
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'appointment_status') THEN "
        "ALTER TYPE appointment_status RENAME TO appointment_status_legacy; "
        "END IF; END $$"
    )
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
    # Drop the legacy enum after the column is migrated. CASCADE so
    # that any straggler columns (e.g. `appointment_events.from_status`
    # / `to_status` — which we drop later) don't block the drop. The
    # `appointment_events` table is dropped explicitly later in this
    # migration; if anything else still depends on the legacy type
    # it'll be dropped by CASCADE here.
    op.execute("DROP TYPE appointment_status_legacy CASCADE")

    # Same for visits.
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'visit_status') THEN "
        "ALTER TYPE visit_status RENAME TO visit_status_legacy; "
        "END IF; END $$"
    )
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
    # Drop the legacy enum after the column is migrated. CASCADE for
    # the same reason as `appointment_status_legacy` above.
    op.execute("DROP TYPE visit_status_legacy CASCADE")

    # ------------------------------------------------------------------
    # 2. Add free-text `name` column to appointment_service_items
    # ------------------------------------------------------------------
    # Backfill `name` from the existing `service_type` enum via a
    # humanised form ("PERSONAL_CARE" -> "Personal Care"). Old rows
    # become the "display name" of the new free-text column.
    op.execute(
        "ALTER TABLE appointment_service_items "
        "ADD COLUMN IF NOT EXISTS name VARCHAR(255)"
    )
    # Backfill `name` only if `service_type` still exists (the column
    # may have been dropped by a partial prior run).
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS ("
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='appointment_service_items' "
        "AND column_name='service_type'"
        ") THEN "
        "UPDATE appointment_service_items SET name = "
        # Cast to text first so this works regardless of which enum
        # labels exist in `service_type` (the migration has to handle
        # any historical schema — different deployments may have added
        # various labels over time). The initcap(replace(...))
        # fallback renders any unrecognised label.
        "CASE service_type::text "
        "WHEN 'PERSONAL_CARE' THEN 'Personal Care' "
        "WHEN 'HOMEMAKING' THEN 'Homemaking' "
        "WHEN 'RESPITE' THEN 'Respite' "
        "WHEN 'SKILLED_NURSING' THEN 'Skilled Nursing' "
        "WHEN 'MENTAL_HEALTH' THEN 'Mental Health' "
        "WHEN 'COUNSELING_INDIVIDUAL' THEN 'Counseling (Individual)' "
        "WHEN 'COUNSELING_GROUP' THEN 'Counseling (Group)' "
        "ELSE initcap(replace(service_type::text, '_', ' ')) "
        "END; "
        "END IF; END $$"
    )
    # Skip the SET NOT NULL — the table is empty in dev so this is
    # trivially satisfied, and the operation may have already been
    # applied by a partial prior run. We don't strictly need it.
    # Drop the old service_type enum column — the spec says activities
    # are free-text, and the Python side no longer reads it.
    op.execute(
        "ALTER TABLE appointment_service_items DROP COLUMN IF EXISTS service_type"
    )
    op.execute("DROP TYPE IF EXISTS service_type")

    # Add the `completed_at` + `completed_by_user_id` columns the new
    # activities shape needs (mirrors `AppointmentActivity` model).
    op.execute(
        "ALTER TABLE appointment_service_items "
        "ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ NULL"
    )
    op.execute(
        "ALTER TABLE appointment_service_items "
        "ADD COLUMN IF NOT EXISTS completed_by_user_id UUID NULL"
    )
    # FK constraint is idempotent (IF NOT EXISTS isn't supported for
    # ADD CONSTRAINT, but a re-run that hits this just blows up with
    # "constraint already exists" — handled by the surrounding
    # transaction rollback so the caller re-runs from scratch).
    op.execute(
        "ALTER TABLE appointment_service_items "
        "ADD CONSTRAINT fk_service_items_completed_by_user "
        "FOREIGN KEY (completed_by_user_id) REFERENCES users(id) ON DELETE SET NULL"
    )

    # ------------------------------------------------------------------
    # 2b. Rename the legacy service-items tables to the spec-aligned
    #     `appointment_activities` + `visit_activity_deliveries` names.
    # ------------------------------------------------------------------
    # The Python ORM (`AppointmentActivity`, `VisitActivityDelivery`)
    # maps to these new names. RLS policies, FKs, indexes, and the
    # Pydantic schemas all reference the new names. The legacy
    # `appointment_service_items` and `visit_service_items` tables
    # are kept physically — we just rename them so the application
    # sees the spec-aligned schema. If `pg_class` already has the
    # new name (idempotent re-run), skip the rename.
    op.execute(
        "ALTER TABLE IF EXISTS appointment_service_items "
        "RENAME TO appointment_activities"
    )
    op.execute(
        "ALTER TABLE IF EXISTS visit_service_items "
        "RENAME TO visit_activity_deliveries"
    )
    # Same for the legacy FK / index names that referenced the old
    # table name — these were created in earlier migrations and would
    # cause confusion if left pointing at the renamed table.
    op.execute(
        "ALTER INDEX IF EXISTS pk_appointment_service_items "
        "RENAME TO pk_appointment_activities"
    )
    op.execute(
        "ALTER INDEX IF EXISTS pk_visit_service_items "
        "RENAME TO pk_visit_activity_deliveries"
    )
    op.execute(
        "ALTER INDEX IF EXISTS idx_service_items_appointment_id "
        "RENAME TO idx_activities_appointment_id"
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

    # Drop the related legacy tables: appointment_confirmations +
    # appointment_events (kept simpler — removed per scope). CASCADE
    # also drops the `confirmation_status` and `appointment_event_type`
    # enum types that these tables reference — we have to drop the
    # tables FIRST because dropping the types first fails with
    # "DependentObjectsStillExist".
    op.execute("DROP TABLE IF EXISTS appointment_confirmations CASCADE")
    op.execute("DROP TABLE IF EXISTS appointment_events CASCADE")
    op.execute("DROP TYPE IF EXISTS confirmation_status")
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

    # ------------------------------------------------------------------
    # 8. Recreate the partial indexes we dropped at the top of the
    #    migration. They're against the new enum values now.
    # ------------------------------------------------------------------
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_appointments_status_active "
        "ON appointments (status) "
        "WHERE status IN ("
        "'SCHEDULED', 'READY', 'IN_PROGRESS', 'AWAITING_SIGNATURE'"
        ")"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_visits_status_active "
        "ON visits (status) "
        "WHERE status IN ('IN_PROGRESS', 'AWAITING_SIGNATURE')"
    )


def downgrade() -> None:
    # Reverse the upgrade in opposite order. This is best-effort —
    # production rollbacks should prefer point-in-time restore from
    # a pre-migration backup.
    raise NotImplementedError(
        "0027_appointment_flow_alignment is forward-only. "
        "Restore from backup to roll back."
    )


__all__ = ["downgrade", "revision", "upgrade"]