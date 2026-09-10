"""Store the map pin directly on every group home.

Revision ID: 0040_group_home_coordinates
Revises: 0039_agency_compliance_operations
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0040_group_home_coordinates"
down_revision = "0039_agency_compliance_operations"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("group_homes", sa.Column("latitude", sa.Numeric(9, 6), nullable=True))
    op.add_column("group_homes", sa.Column("longitude", sa.Numeric(9, 6), nullable=True))
    op.execute("""UPDATE group_homes gh SET latitude = l.latitude, longitude = l.longitude FROM locations l WHERE l.id = gh.location_id""")
    op.execute("UPDATE group_homes SET latitude = 0, longitude = 0 WHERE latitude IS NULL OR longitude IS NULL")
    op.alter_column("group_homes", "latitude", nullable=False)
    op.alter_column("group_homes", "longitude", nullable=False)
    op.create_check_constraint("ck_group_homes_latitude_range", "group_homes", "latitude BETWEEN -90 AND 90")
    op.create_check_constraint("ck_group_homes_longitude_range", "group_homes", "longitude BETWEEN -180 AND 180")

def downgrade() -> None:
    op.drop_constraint("ck_group_homes_longitude_range", "group_homes", type_="check")
    op.drop_constraint("ck_group_homes_latitude_range", "group_homes", type_="check")
    op.drop_column("group_homes", "longitude")
    op.drop_column("group_homes", "latitude")
