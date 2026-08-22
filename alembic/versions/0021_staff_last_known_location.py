"""Add staff last-known location fields for the EVV Live Monitor.

The EVV Live Monitor today is visit-scoped: it renders one map pin per
in-progress visit, fed by `visits.live_lat/lng`. Staff who have just
clocked out (or who haven't checked in yet) are invisible on the admin
map — even though the system knows where they were 30 seconds ago.

This migration extends `staff_profiles` with a denormalised last-known
location (`last_known_lat/lng/accuracy_m/ping_at/device_id/visit_id`).
The existing `POST /visits/{id}/location-ping` writer is extended in
the same change to populate these columns alongside the visit-level
fields, so the staff-level view is a *separate* read view that doesn't
require a join or a recent visit.

Only the most recent ping is retained — mirrors the product decision
for visit-level live location (see migration 0017).

No new index: `staff_profiles` is already keyed by `agency_id` for the
existing roster endpoints, and the new live-locations endpoint filters
on `agency_id` + `last_known_ping_at >= now() - interval`. If this
turns out to be slow in production we'll add a partial index, but
adding one speculatively would cost more than it saves on a per-visit
write hot path.

RLS on `staff_profiles` (migration 0004) is row-level and scopes reads
by `agency_id`. Adding nullable columns doesn't change the predicate,
so existing policies continue to apply without modification.

Revision ID: 0021_staff_last_known_location
Revises: 0019_patient_and_appointment_location
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision: str = "0021_staff_last_known_location"
down_revision: str | Sequence[str] | None = "0019_patient_and_appointment_location"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ----- staff_profiles: last-known GPS -----
    op.add_column(
        "staff_profiles",
        sa.Column("last_known_lat", sa.Numeric(precision=9, scale=6), nullable=True),
    )
    op.add_column(
        "staff_profiles",
        sa.Column("last_known_lng", sa.Numeric(precision=9, scale=6), nullable=True),
    )
    op.add_column(
        "staff_profiles",
        sa.Column(
            "last_known_accuracy_m",
            sa.Numeric(precision=6, scale=2),
            nullable=True,
        ),
    )
    op.add_column(
        "staff_profiles",
        sa.Column(
            "last_known_ping_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "staff_profiles",
        sa.Column("last_known_device_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "staff_profiles",
        sa.Column(
            "last_known_visit_id",
            UUID(as_uuid=True),
            sa.ForeignKey("visits.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # ----- lat/lng range + pair constraints (mirror patient_profiles) -----
    op.create_check_constraint(
        "ck_staff_last_lat_range",
        "staff_profiles",
        "last_known_lat IS NULL OR last_known_lat BETWEEN -90 AND 90",
    )
    op.create_check_constraint(
        "ck_staff_last_lng_range",
        "staff_profiles",
        "last_known_lng IS NULL OR last_known_lng BETWEEN -180 AND 180",
    )
    op.create_check_constraint(
        "ck_staff_last_lat_lng_pair",
        "staff_profiles",
        "(last_known_lat IS NULL) = (last_known_lng IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_staff_last_lat_lng_pair", "staff_profiles", type_="check")
    op.drop_constraint("ck_staff_last_lng_range", "staff_profiles", type_="check")
    op.drop_constraint("ck_staff_last_lat_range", "staff_profiles", type_="check")
    op.drop_column("staff_profiles", "last_known_visit_id")
    op.drop_column("staff_profiles", "last_known_device_id")
    op.drop_column("staff_profiles", "last_known_ping_at")
    op.drop_column("staff_profiles", "last_known_accuracy_m")
    op.drop_column("staff_profiles", "last_known_lng")
    op.drop_column("staff_profiles", "last_known_lat")
