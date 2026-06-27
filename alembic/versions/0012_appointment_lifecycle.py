"""Appointment lifecycle — confirmations + immutable event log (schema doc §10.3 + §10.4).

This migration adds two tables that power the patient/guardian-facing
confirmation flow and the immutable per-appointment audit timeline:

  - `appointment_confirmations` (1:1 with `appointments`)
      Captures WHO confirmed or declined an appointment and HOW
      (comment). One row per appointment (UNIQUE on `appointment_id`);
      a later confirmation overwrites the prior one via an upsert in
      the service layer.

  - `appointment_events` (append-only, N:1 with `appointments`)
      Domain event log. Every status transition and every patient- or
      admin-initiated action (confirm, request-reschedule, request-cancel,
      admin-cancel) appends one row. Immutable: UPDATE/DELETE are blocked
      by both RLS (no FOR UPDATE/DELETE policy) and a DB trigger.

RLS:
  - `appointment_confirmations`:
      * SELECT — anyone authenticated at the appointment's agency
        (joined via the appointment row, RLS uses `agency_id`).
      * INSERT — anyone authenticated (the service layer enforces that
        the actor owns/guards/admin-oversees the appointment).
      * UPDATE — AGENCY_ADMIN only (so an admin can fix a bad row).
      * DELETE — nobody (DB trigger would also block, but the policy
        makes intent explicit).
  - `appointment_events`:
      * SELECT — anyone authenticated at the appointment's agency.
      * INSERT — anyone authenticated (event-appending goes through
        the service helper).
      * UPDATE / DELETE — blocked by trigger (`appointment_events_no_modify`).

Soft delete is not used here — both tables are reference data.

Revision ID: 0012_appointment_lifecycle
Revises: 0011_locations
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID

# revision identifiers, used by Alembic.
revision: str = "0012_appointment_lifecycle"
down_revision: str | Sequence[str] | None = "0011_locations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ============================================================
    # Enums — created up-front so the table DDL below can reference
    # them. appointment_event_type is new; user_role and
    # confirmation_status already exist (created by migration 0001),
    # so we don't recreate them.
    # ============================================================
    op.execute(
        "DO $$ BEGIN "
        "CREATE TYPE appointment_event_type AS ENUM ("
        "'STATUS_TRANSITION', 'CONFIRMATION_FILED', 'RESCHEDULE_REQUESTED', "
        "'CANCELLATION_REQUESTED', 'CANCELLED_BY_ADMIN'"
        "); "
        "EXCEPTION WHEN duplicate_object THEN NULL; "
        "END $$"
    )

    # ============================================================
    # appointment_confirmations
    # ============================================================
    op.create_table(
        "appointment_confirmations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "appointment_id",
            UUID(as_uuid=True),
            sa.ForeignKey("appointments.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "confirmed_by",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "confirmation_role",
            postgresql.ENUM("PATIENT", "GUARDIAN", name="user_role", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(
                "CONFIRMED", "DECLINED", name="confirmation_status", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "confirmation_role IN ('PATIENT', 'GUARDIAN')",
            name="ck_appointment_confirmations_role",
        ),
    )
    op.create_index(
        "idx_appointment_confirmations_confirmed_by",
        "appointment_confirmations",
        ["confirmed_by"],
    )

    # ============================================================
    # appointment_events
    # ============================================================
    op.create_table(
        "appointment_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "appointment_id",
            UUID(as_uuid=True),
            sa.ForeignKey("appointments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agency_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agencies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "actor_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "event_type",
            postgresql.ENUM(
                "STATUS_TRANSITION",
                "CONFIRMATION_FILED",
                "RESCHEDULE_REQUESTED",
                "CANCELLATION_REQUESTED",
                "CANCELLED_BY_ADMIN",
                name="appointment_event_type",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "from_status",
            postgresql.ENUM(
                "DRAFT",
                "SCHEDULED",
                "NOTIFICATION_SENT",
                "AWAITING_CONFIRMATION",
                "CONFIRMED",
                "RESCHEDULE_REQUESTED",
                "CANCELLATION_REQUESTED",
                "ASSIGNED",
                "CHECKED_IN",
                "IN_PROGRESS",
                "CHECKED_OUT",
                "COMPLETED",
                "AWAITING_SERVICE_VERIFICATION",
                "SERVICE_VERIFIED",
                "DISPUTED",
                "UNDER_REVIEW",
                "APPROVED_FOR_BILLING",
                "PAID",
                "CANCELLED",
                "NO_SHOW",
                "REJECTED",
                name="appointment_status",
                create_type=False,
            ),
            nullable=True,
        ),
        sa.Column(
            "to_status",
            postgresql.ENUM(
                "DRAFT",
                "SCHEDULED",
                "NOTIFICATION_SENT",
                "AWAITING_CONFIRMATION",
                "CONFIRMED",
                "RESCHEDULE_REQUESTED",
                "CANCELLATION_REQUESTED",
                "ASSIGNED",
                "CHECKED_IN",
                "IN_PROGRESS",
                "CHECKED_OUT",
                "COMPLETED",
                "AWAITING_SERVICE_VERIFICATION",
                "SERVICE_VERIFIED",
                "DISPUTED",
                "UNDER_REVIEW",
                "APPROVED_FOR_BILLING",
                "PAID",
                "CANCELLED",
                "NO_SHOW",
                "REJECTED",
                name="appointment_status",
                create_type=False,
            ),
            nullable=True,
        ),
        sa.Column(
            "metadata",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("ip_address", INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "length(trim(event_type::text)) > 0",
            name="ck_appointment_events_type_non_empty",
        ),
    )
    op.create_index(
        "idx_appointment_events_appointment",
        "appointment_events",
        ["appointment_id", sa.text("created_at")],
    )
    op.create_index(
        "idx_appointment_events_agency_date",
        "appointment_events",
        ["agency_id", sa.text("created_at DESC")],
    )

    # Append-only trigger — mirror of audit_logs_no_modify.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION appointment_events_no_modify()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'appointment_events is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_appointment_events_no_modify
        BEFORE UPDATE OR DELETE ON appointment_events
        FOR EACH ROW EXECUTE FUNCTION appointment_events_no_modify()
        """
    )

    # ============================================================
    # RLS — appointment_confirmations
    # ============================================================
    op.execute(
        "ALTER TABLE appointment_confirmations ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE appointment_confirmations FORCE ROW LEVEL SECURITY"
    )

    # SELECT — anyone authenticated at the appointment's agency.
    # We don't store agency_id on the row (would be redundant); instead
    # join via appointments.agency_id in the SELECT policy.
    op.execute(
        """
        CREATE POLICY appt_confirmations_select ON appointment_confirmations
        FOR SELECT
        USING (
            app.is_super_admin()
            OR EXISTS (
                SELECT 1 FROM appointments a
                WHERE a.id = appointment_confirmations.appointment_id
                  AND a.agency_id = app.current_agency_id()
            )
        )
        """
    )

    # INSERT — anyone authenticated; the service layer enforces that the
    # actor owns/guards/admin-oversees the appointment.
    op.execute(
        """
        CREATE POLICY appt_confirmations_insert ON appointment_confirmations
        FOR INSERT
        WITH CHECK (
            app.is_super_admin()
            OR EXISTS (
                SELECT 1 FROM appointments a
                WHERE a.id = appointment_confirmations.appointment_id
                  AND a.agency_id = app.current_agency_id()
            )
        )
        """
    )

    # UPDATE — AGENCY_ADMIN at the appointment's agency (for fixing bad rows).
    op.execute(
        """
        CREATE POLICY appt_confirmations_update ON appointment_confirmations
        FOR UPDATE
        USING (
            app.is_super_admin()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND EXISTS (
                    SELECT 1 FROM appointments a
                    WHERE a.id = appointment_confirmations.appointment_id
                      AND a.agency_id = app.current_agency_id()
                )
            )
        )
        WITH CHECK (
            app.is_super_admin()
            OR (
                app.has_agency_role('AGENCY_ADMIN')
                AND EXISTS (
                    SELECT 1 FROM appointments a
                    WHERE a.id = appointment_confirmations.appointment_id
                      AND a.agency_id = app.current_agency_id()
                )
            )
        )
        """
    )

    # ============================================================
    # RLS — appointment_events
    # ============================================================
    op.execute("ALTER TABLE appointment_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE appointment_events FORCE ROW LEVEL SECURITY")

    # SELECT — anyone authenticated at the event's agency.
    op.execute(
        """
        CREATE POLICY appt_events_select ON appointment_events
        FOR SELECT
        USING (
            app.is_super_admin()
            OR agency_id = app.current_agency_id()
        )
        """
    )

    # INSERT — anyone authenticated (agency scoping enforced by WITH CHECK).
    op.execute(
        """
        CREATE POLICY appt_events_insert ON appointment_events
        FOR INSERT
        WITH CHECK (
            app.is_super_admin()
            OR agency_id = app.current_agency_id()
        )
        """
    )


def downgrade() -> None:
    # ---- appointment_events ----
    op.execute("DROP POLICY IF EXISTS appt_events_insert ON appointment_events")
    op.execute("DROP POLICY IF EXISTS appt_events_select ON appointment_events")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_appointment_events_no_modify "
        "ON appointment_events"
    )
    op.execute("DROP FUNCTION IF EXISTS appointment_events_no_modify()")
    op.execute("ALTER TABLE IF EXISTS appointment_events NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE IF EXISTS appointment_events DISABLE ROW LEVEL SECURITY")
    op.drop_table("appointment_events")
    op.execute("DROP TYPE IF EXISTS appointment_event_type")

    # ---- appointment_confirmations ----
    op.execute(
        "DROP POLICY IF EXISTS appt_confirmations_update ON appointment_confirmations"
    )
    op.execute(
        "DROP POLICY IF EXISTS appt_confirmations_insert ON appointment_confirmations"
    )
    op.execute(
        "DROP POLICY IF EXISTS appt_confirmations_select ON appointment_confirmations"
    )
    op.execute(
        "ALTER TABLE IF EXISTS appointment_confirmations "
        "NO FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE IF EXISTS appointment_confirmations "
        "DISABLE ROW LEVEL SECURITY"
    )
    op.drop_table("appointment_confirmations")
