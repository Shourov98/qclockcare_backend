"""Add live-location fields to `visits` for the EVV Live Monitor.

When a staff member is mid-visit they can opt into live GPS sharing;
their browser sends `POST /visits/{id}/location-ping` every ~15s with
the current lat/lng. We persist only the most recent ping on the visit
row (no history table — per product decision) and add a partial index
on `live_ping_at` filtered to `sharing_location = true` so the EVV
page's "show all live visits at this agency" query stays fast.

Revision ID: 0017_visit_live_location
Revises: 0016_appointment_charges
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0017_visit_live_location"
down_revision: str | Sequence[str] | None = "0016_appointment_charges"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ----- visits: live GPS state -----
    op.add_column(
        "visits",
        sa.Column("live_lat", sa.Numeric(precision=9, scale=6), nullable=True),
    )
    op.add_column(
        "visits",
        sa.Column("live_lng", sa.Numeric(precision=9, scale=6), nullable=True),
    )
    op.add_column(
        "visits",
        sa.Column(
            "live_ping_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "visits",
        sa.Column("live_accuracy_m", sa.Numeric(precision=6, scale=2), nullable=True),
    )
    op.add_column(
        "visits",
        sa.Column(
            "sharing_location",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # Partial index — speeds up the EVV "show me visits currently
    # sharing" query without bloating the index with null rows.
    op.create_index(
        "idx_visits_live_ping",
        "visits",
        ["agency_id", sa.text("live_ping_at DESC")],
        postgresql_where=sa.text("sharing_location = true"),
    )


def downgrade() -> None:
    op.drop_index("idx_visits_live_ping", table_name="visits")
    op.drop_column("visits", "sharing_location")
    op.drop_column("visits", "live_accuracy_m")
    op.drop_column("visits", "live_ping_at")
    op.drop_column("visits", "live_lng")
    op.drop_column("visits", "live_lat")
