"""Drop structured home-address + per-appointment location overrides.

Rolls back everything added by `0019_patient_and_appointment_location`.
The product direction changed — visit completion is now driven by
patient/guardian confirmation, not GPS verification, so a
patient/appointment geofence is no longer needed. Staff-side GPS
sharing is unaffected (see migrations `0017_visit_live_location` and
`0021_staff_last_known_location`).

Drops:

- `patient_profiles.address_line1`, `address_line2`, `city`, `state`,
  `postal_code`, `country`, `home_lat`, `home_lng` (8 cols) + their
  5 check constraints.
- `appointments.location_lat`, `location_lng`, `location_address`,
  `location_source`, `geofence_radius_m` (5 cols) + their 5 check
  constraints.

RLS policies on both tables are row-level (filter on `agency_id`/
`user_id`); dropping nullable columns doesn't change the predicate, so
existing SELECT/MODIFY policies continue to apply without modification.

Revision ID: 0022_drop_patient_and_appointment_location
Revises: 0021_staff_last_known_location
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0022_drop_patient_and_appointment_location"
down_revision: str | Sequence[str] | None = "0021_staff_last_known_location"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ============================================================
    # appointments: drop structured-location override columns
    # ============================================================
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

    # ============================================================
    # patient_profiles: drop structured home-address columns
    # ============================================================
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


def downgrade() -> None:
    """Re-add the columns + constraints in the same shape `0019` used."""
    import sqlalchemy as sa

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
