"""Locations table (schema doc §9).

A `location` is a service-delivery address that an agency uses for
appointments and visits. Typical labels are "Home", "Day Program",
"Office", "Group Home". Each location carries:

  - A postal address (line1/line2/city/state/postal/country).
  - Optional lat/lng + geofence radius (used by the visit check-in
    flow to determine whether a staff member is at the location).
  - `is_active` flag — admins can deactivate a location without
    deleting its history. Inactive locations can't be used for new
    appointments but historical ones remain readable.

RLS:
  - SUPER_ADMIN: full access (cross-agency ops).
  - AGENCY_ADMIN at the agency: full access for their agency's rows.
  - STAFF at the agency: SELECT only — they need to read locations
    to know where to go. No writes.
  - PATIENT / GUARDIAN: cannot read raw location rows; they receive
    address strings only via the patient/visit surface when needed.

Soft delete is via `deleted_at`. The unique-per-agency index is on
`(agency_id, label, deleted_at)` — multiple locations at the same
agency can share a label if we soft-delete one first (the partial
index excludes deleted rows).

Revision ID: 0011_locations
Revises: 0010_notifications_enhancements
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011_locations"
down_revision: str | Sequence[str] | None = "0010_notifications_enhancements"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ============================================================
    # locations
    # ============================================================
    op.create_table(
        "locations",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "agency_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agencies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("address_line1", sa.Text(), nullable=False),
        sa.Column("address_line2", sa.Text(), nullable=True),
        sa.Column("city", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("postal_code", sa.Text(), nullable=False),
        sa.Column(
            "country",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'US'"),
        ),
        sa.Column("latitude", sa.Numeric(9, 6), nullable=True),
        sa.Column("longitude", sa.Numeric(9, 6), nullable=True),
        sa.Column(
            "geofence_radius_m",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("150"),
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(trim(state)) = 2",
            name="ck_locations_state_two_letters",
        ),
        sa.CheckConstraint(
            "length(trim(postal_code)) > 0",
            name="ck_locations_postal_code_non_empty",
        ),
        sa.CheckConstraint(
            "(latitude IS NULL AND longitude IS NULL) OR "
            "(latitude IS NOT NULL AND longitude IS NOT NULL "
            "AND latitude BETWEEN -90 AND 90 "
            "AND longitude BETWEEN -180 AND 180)",
            name="ck_locations_lat_lng_pair",
        ),
        sa.CheckConstraint(
            "geofence_radius_m BETWEEN 10 AND 5000",
            name="ck_locations_geofence_radius_range",
        ),
    )
    op.create_index(
        "idx_locations_agency_id",
        "locations",
        ["agency_id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "idx_locations_agency_label",
        "locations",
        ["agency_id", "label"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.execute(
        "CREATE TRIGGER trg_locations_updated_at "
        "BEFORE UPDATE ON locations "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    # ============================================================
    # RLS
    # ============================================================
    op.execute("ALTER TABLE locations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE locations FORCE ROW LEVEL SECURITY")

    # SELECT — anyone authenticated at the agency may read; SUPER_ADMIN
    # bypasses for cross-agency ops.
    op.execute(
        """
        CREATE POLICY locations_select ON locations
        FOR SELECT
        USING (
            app.is_super_admin()
            OR agency_id = app.current_agency_id()
        )
        """
    )

    # INSERT / UPDATE / DELETE — AGENCY_ADMIN at the agency only.
    op.execute(
        """
        CREATE POLICY locations_modify ON locations
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
    # RLS
    op.execute("DROP POLICY IF EXISTS locations_modify ON locations")
    op.execute("DROP POLICY IF EXISTS locations_select ON locations")

    # Trigger
    op.execute("DROP TRIGGER IF EXISTS trg_locations_updated_at ON locations")

    # Table
    op.execute("ALTER TABLE IF EXISTS locations NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE IF EXISTS locations DISABLE ROW LEVEL SECURITY")
    op.drop_table("locations")
