"""Link `appointments` to the `locations` table.

Why
---
Each appointment currently stores the visit address as a free-text
`location` column only. The FE now wants to render a map pin and
distance-from-clinic badge, both of which need lat/lng. The
`locations` table already carries those — it was created in
migration 0011 with `latitude` / `longitude` / `address_line1` /
`city` / `state` / `postal_code` and a default `geofence_radius_m`.

This migration:

  1. Adds `appointments.location_id` (UUID, nullable, FK to
     `locations.id` ON DELETE SET NULL). Nullable so:
       - existing rows with a free-text `location` keep working,
       - new appointments without a structured location can still
         be scheduled (the free-text fallback stays in place).
  2. Adds an index on `(agency_id, location_id)` so the per-agency
     "list appointments at this location" query stays fast.

We deliberately do NOT add a CHECK constraint requiring the FK row
to live in the same agency: Postgres doesn't allow subqueries in
CHECK constraints, and the application layer already enforces the
agency boundary (the `get_appointment` service function takes an
`agency_id` and the RLS policy filters by it). A misplaced FK row
would still hit the RLS wall on read.

We deliberately do NOT add the per-row `location_lat` /
`location_lng` / `location_address` / `location_source` /
`geofence_radius_m` columns that migration 0019 once added and
0022 removed. That whole feature was rolled back because the
product moved away from geofenced EVV verification — but a
read-only map pin is still useful for the FE calendar. Linking
to the existing `locations` table gives us lat/lng with zero
schema duplication.

RLS: `appointments` already has row-level policies filtering on
`agency_id`. The new column is nullable, so existing policies
are unchanged — they just expose one more field to permitted
callers.

Revision ID: 0033_appointment_location_id
Revises:    0032_audit_action_enum_values
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0033_appointment_location_id"
down_revision: str | Sequence[str] | None = "0032_audit_action_enum_values"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "appointments",
        sa.Column(
            "location_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_appointments_location_id_locations",
        "appointments",
        "locations",
        ["location_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "idx_appointments_agency_location",
        "appointments",
        ["agency_id", "location_id"],
        postgresql_where=sa.text("location_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "idx_appointments_agency_location", table_name="appointments"
    )
    op.drop_constraint(
        "fk_appointments_location_id_locations", "appointments", type_="foreignkey"
    )
    op.drop_column("appointments", "location_id")
