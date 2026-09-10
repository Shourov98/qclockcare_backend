"""Remove the stale NOT NULL legacy service_type activity column.

Revision ID: 0041_drop_legacy_activity_service_type
Revises: 0040_group_home_coordinates
"""

from __future__ import annotations

from alembic import op

revision = "0041_drop_legacy_activity_service_type"
down_revision = "0040_group_home_coordinates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Activities were migrated to free-text `name` in revision 0027. Some
    # existing databases retained this legacy NOT NULL column, causing every
    # current API insert to fail despite a valid `name` and duration.
    op.execute("ALTER TABLE appointment_activities DROP COLUMN IF EXISTS service_type")
    op.execute("DROP TYPE IF EXISTS service_type")


def downgrade() -> None:
    # A populated legacy enum cannot be reconstructed from arbitrary
    # free-text names, so this corrective migration is intentionally
    # irreversible.
    pass
