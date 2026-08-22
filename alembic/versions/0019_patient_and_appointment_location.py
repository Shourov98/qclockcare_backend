"""Add structured home-address + per-appointment location overrides.

Adds:

- `patient_profiles.address_line1`, `address_line2`, `city`,
  `state`, `postal_code`, `country`  (Text-ish, all nullable) +
  `home_lat`, `home_lng` (Numeric(9, 6), nullable).
- `appointments.location_lat`, `location_lng` (Numeric(9, 6),
  nullable), `location_address` (Text), `location_source`
  (PATIENT_HOME | OVERRIDE | INHERITED, nullable),
  `geofence_radius_m` (Integer, nullable).

Why these columns:
- `patient_profiles.*` makes the patient's home address a first-class
  field that appointments can inherit.
- `appointments.location_*` lets a visit override the patient's home
  location (e.g. "this Tuesday's visit is at the doctor's office").
- `appointments.location_source` records *how* the row got its
  coordinates — useful for audit + UI badges ("Inherited from
  patient" / "Overridden").
- `appointments.geofence_radius_m` lets a single visit relax or
  tighten the geofence around its specific location.

Constraints:
- `home_lat`/`home_lng` must be set together or both NULL.
- `location_lat`/`location_lng` must be set together or both NULL.
- Lat ∈ [-90, 90], lng ∈ [-180, 180].
- `state` and `country` are 2-letter codes (matching the existing
  `Location` model from migration 0011).
- `location_source` ∈ {PATIENT_HOME, OVERRIDE, INHERITED} when set.
- `geofence_radius_m` ∈ [10, 5000] when set (matches `Location` model).

RLS policies on both tables are row-level (filter on `agency_id`/`user_id`)
so adding nullable columns does not require policy changes — existing
SELECT/MODIFY policies continue to expose new fields automatically.

Revision ID: 0019_patient_and_appointment_location
Revises: 0018_report_runs
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0019_patient_and_appointment_location"
down_revision: str | Sequence[str] | None = "0018_report_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ============================================================
    # Widen alembic_version.version_num first
    # ============================================================
    # The default Alembic version table is created with VARCHAR(32).
    # This revision id is 36 chars, so the post-migration UPDATE that
    # records the new revision id would fail with a string-truncation
    # error unless we widen the column first. Doing it here (rather
    # than in a separate migration) keeps the chain linear and avoids
    # the same read-then-rename hazard.
    op.execute("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64)")

    # ============================================================
    # patient_profiles: structured home address + coordinates
    # ============================================================
    op.add_column(
        "patient_profiles",
        sa.Column("address_line1", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "patient_profiles",
        sa.Column("address_line2", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "patient_profiles",
        sa.Column("city", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "patient_profiles",
        sa.Column("state", sa.String(length=2), nullable=True),
    )
    op.add_column(
        "patient_profiles",
        sa.Column("postal_code", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "patient_profiles",
        sa.Column(
            "country",
            sa.String(length=2),
            nullable=True,
            server_default=sa.text("'US'"),
        ),
    )
    op.add_column(
        "patient_profiles",
        sa.Column("home_lat", sa.Numeric(precision=9, scale=6), nullable=True),
    )
    op.add_column(
        "patient_profiles",
        sa.Column("home_lng", sa.Numeric(precision=9, scale=6), nullable=True),
    )

    op.create_check_constraint(
        "ck_patient_state_two_letters",
        "patient_profiles",
        "state IS NULL OR length(trim(state)) = 2",
    )
    op.create_check_constraint(
        "ck_patient_country_two_letters",
        "patient_profiles",
        "country IS NULL OR length(trim(country)) = 2",
    )
    op.create_check_constraint(
        "ck_patient_home_lat_range",
        "patient_profiles",
        "home_lat IS NULL OR (home_lat >= -90 AND home_lat <= 90)",
    )
    op.create_check_constraint(
        "ck_patient_home_lng_range",
        "patient_profiles",
        "home_lng IS NULL OR (home_lng >= -180 AND home_lng <= 180)",
    )
    op.create_check_constraint(
        "ck_patient_home_lat_lng_pair",
        "patient_profiles",
        "(home_lat IS NULL) = (home_lng IS NULL)",
    )

    # ============================================================
    # appointments: location override + geofence radius
    # ============================================================
    op.add_column(
        "appointments",
        sa.Column("location_lat", sa.Numeric(precision=9, scale=6), nullable=True),
    )
    op.add_column(
        "appointments",
        sa.Column("location_lng", sa.Numeric(precision=9, scale=6), nullable=True),
    )
    op.add_column(
        "appointments",
        sa.Column("location_address", sa.Text(), nullable=True),
    )
    op.add_column(
        "appointments",
        sa.Column("location_source", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "appointments",
        sa.Column("geofence_radius_m", sa.Integer(), nullable=True),
    )

    op.create_check_constraint(
        "ck_appointment_loc_lat_range",
        "appointments",
        "location_lat IS NULL OR (location_lat >= -90 AND location_lat <= 90)",
    )
    op.create_check_constraint(
        "ck_appointment_loc_lng_range",
        "appointments",
        "location_lng IS NULL OR (location_lng >= -180 AND location_lng <= 180)",
    )
    op.create_check_constraint(
        "ck_appointment_loc_pair",
        "appointments",
        "(location_lat IS NULL) = (location_lng IS NULL)",
    )
    op.create_check_constraint(
        "ck_appointment_loc_source",
        "appointments",
        (
            "location_source IS NULL OR "
            "location_source IN ('PATIENT_HOME', 'OVERRIDE', 'INHERITED')"
        ),
    )
    op.create_check_constraint(
        "ck_appointment_geofence_radius_range",
        "appointments",
        "geofence_radius_m IS NULL OR "
        "(geofence_radius_m >= 10 AND geofence_radius_m <= 5000)",
    )


def downgrade() -> None:
    # appointments
    op.drop_constraint(
        "ck_appointment_geofence_radius_range", "appointments", type_="check"
    )
    op.drop_constraint("ck_appointment_loc_source", "appointments", type_="check")
    op.drop_constraint("ck_appointment_loc_pair", "appointments", type_="check")
    op.drop_constraint("ck_appointment_loc_lng_range", "appointments", type_="check")
    op.drop_constraint("ck_appointment_loc_lat_range", "appointments", type_="check")
    op.drop_column("appointments", "geofence_radius_m")
    op.drop_column("appointments", "location_source")
    op.drop_column("appointments", "location_address")
    op.drop_column("appointments", "location_lng")
    op.drop_column("appointments", "location_lat")

    # patient_profiles
    op.drop_constraint(
        "ck_patient_home_lat_lng_pair", "patient_profiles", type_="check"
    )
    op.drop_constraint(
        "ck_patient_home_lng_range", "patient_profiles", type_="check"
    )
    op.drop_constraint(
        "ck_patient_home_lat_range", "patient_profiles", type_="check"
    )
    op.drop_constraint(
        "ck_patient_country_two_letters", "patient_profiles", type_="check"
    )
    op.drop_constraint(
        "ck_patient_state_two_letters", "patient_profiles", type_="check"
    )
    op.drop_column("patient_profiles", "home_lng")
    op.drop_column("patient_profiles", "home_lat")
    op.drop_column("patient_profiles", "country")
    op.drop_column("patient_profiles", "postal_code")
    op.drop_column("patient_profiles", "state")
    op.drop_column("patient_profiles", "city")
    op.drop_column("patient_profiles", "address_line2")
    op.drop_column("patient_profiles", "address_line1")