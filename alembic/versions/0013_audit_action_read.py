"""Add `READ` value to the `audit_action` Postgres ENUM.

The Python `AuditAction` enum (src/shared/domain/enums.py) gains a
new `READ` member for log-row INSERTs that record a read-only access
(e.g. a staff member downloading a qualification document via a
signed URL, a guardian viewing their linked patient's profile).

The column itself (`audit_logs.action`) is already declared as the
`audit_action` ENUM in 0001_base.py / 0009_audit_logs.py and uses
the same Python enum for validation — so the only DB-side change
required is extending the ENUM with the new value.

`ALTER TYPE … ADD VALUE IF NOT EXISTS` is idempotent (safe to re-run)
and works inside transactions on PostgreSQL 12+. No downgrade is
provided: Postgres does not support `DROP VALUE` on an ENUM, and
removing the type entirely would orphan any rows that reference it.
Reverting this migration would require a manual backfill + DROP +
CREATE — out of scope for a routine enum extension.

Revision ID: 0013_audit_action_read
Revises: 0012_appointment_lifecycle
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013_audit_action_read"
down_revision: str | Sequence[str] | None = "0012_appointment_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Idempotent — `IF NOT EXISTS` was added in Postgres 9.6. The
    # project's docker-compose pin is Postgres 16, so this is safe.
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'READ'")


def downgrade() -> None:
    # Postgres does not support `DROP VALUE` on an ENUM type. The
    # only ways to "remove" READ are:
    #   1. Rename the old type, create a new one without READ, then
    #      ALTER TABLE … ALTER COLUMN … TYPE new_type USING old::new_type.
    #   2. Backfill any READ rows to a fallback value (e.g. CREATE)
    #      first, then do (1).
    # Both are out of scope for this routine enum extension — a
    # future PR that wants to actually retire `READ` should write
    # that migration explicitly. For now, the upgrade is irreversible.
    pass
