"""Make notification_preferences.agency_id nullable for cross-tenant admins.

The cross-tenant admin dashboard at farhan-salad-admin is gated to
SUPER_ADMIN (and, after this change, the new PLATFORM_ADMIN role).
Those users have `agency_id = NULL` on their user rows and were
previously blocked from `/notifications/preferences` entirely.

This migration relaxes the column to allow NULL. The composite primary
key `(user_id, type, channel)` already correctly identifies rows
without needing agency_id, so cross-tenant rows simply have
`agency_id IS NULL`.

The existing `idx_notification_prefs_agency` index is dropped — it
cannot be used for `agency_id IS NULL` lookups efficiently on its own.
The remaining `idx_notification_prefs_user` and the composite PK serve
all query paths:

  - Agency admin:      filter by user_id (PK), filter by agency_id if needed
  - Cross-tenant admin: filter by user_id (PK)

If agency-scoped queries turn out to be slow for cross-tenant rows
(impossible — they have agency_id NULL), revisit. For now: keep it
simple.

Revision ID: 0023_notification_prefs_agency_nullable
Revises: 0022_drop_patient_and_appointment_location
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_notification_prefs_agency_nullable"
down_revision = "0022_drop_patient_and_appointment_location"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        "idx_notification_prefs_agency",
        table_name="notification_preferences",
    )
    op.alter_column(
        "notification_preferences",
        "agency_id",
        existing_type=sa.dialects.postgresql.UUID(),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "notification_preferences",
        "agency_id",
        existing_type=sa.dialects.postgresql.UUID(),
        nullable=False,
    )
    op.create_index(
        "idx_notification_prefs_agency",
        "notification_preferences",
        ["agency_id", "type"],
    )
