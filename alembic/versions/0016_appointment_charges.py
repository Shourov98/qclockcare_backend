"""Appointment charges — stub migration.

The original work tracked in this revision id (adding charge fields to
appointments) was applied via a one-off SQL patch against Supabase
before Alembic was wired into the deploy pipeline. No schema change
ships with this revision — it exists only so the migration graph is
walkable and `0017_visit_live_location` can declare it as its parent.

If/when the `appointment_charges` work is ported to a real migration,
move the schema changes here and bump the `down_revision` of
`0017_visit_live_location` accordingly. Today, this is a no-op so
`alembic upgrade head` can run end-to-end.

Revision ID: 0016_appointment_charges
Revises: 0015_billing_stripe_fields
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0016_appointment_charges"
down_revision: str | Sequence[str] | None = "0015_billing_stripe_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op: charge fields were applied outside the migration pipeline."""


def downgrade() -> None:
    """No-op."""


__all__: list[str] = []
