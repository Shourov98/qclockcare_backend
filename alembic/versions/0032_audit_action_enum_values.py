"""Sync the Postgres enum types with the Python enum classes.

Why
---
Several Postgres enum types drifted out of sync with their Python
counterparts — Alembic created the enums but every subsequent enum
*value* addition in Python was forgotten at the DB layer. Three
endpoints hit the gap on the patient smoke test:

  - `POST /portal/support/tickets` → 500 `invalid input value for enum
    audit_action: "SUPPORT_TICKET_OPENED"` (the support module stamps
    that audit row on ticket open).
  - `GET /notifications/preferences` → 500 `invalid input value for enum
    notification_type: "VISIT_STARTED"` (the route backfills defaults
    for the (type, channel) matrix; one of the matrix values references
    a Python enum value the DB enum doesn't have).
  - The compliance issue queue + visit lifecycle audit stamps would
    fail with `COMPLIANCE_ISSUE_*` / `VISIT_*` / `ACTIVITY_MARKED_*`
    not found.

What
----
`ALTER TYPE <name> ADD VALUE IF NOT EXISTS '...'` for every value
declared in the Python enum but absent from the Postgres type.
`IF NOT EXISTS` makes this safe to re-run.

Revision ID: 0032_audit_action_enum_values
Revises:    0031_visit_activity_deliveries_activity_id_rename
"""

from __future__ import annotations

from alembic import op


revision = "0032_audit_action_enum_values"
down_revision = "0031_visit_activity_deliveries_activity_id_rename"
branch_labels = None
depends_on = None


# --------------------------------------------------------------------------
# audit_action values (Python: class AuditAction(StrEnum))
# --------------------------------------------------------------------------
_AUDIT_ACTION_VALUES = [
    # Auth lifecycle additions
    "APPOINTMENT_MARKED_READY",
    # Visit lifecycle additions
    "VISIT_STARTED",
    "VISIT_SUBMITTED_FOR_SIGNATURE",
    "VISIT_SIGNED",
    "VISIT_COMPLETED",
    "BILLING_CONFIRMED",
    "ACTIVITY_MARKED_DONE",
    "ACTIVITY_MARKED_NOT_DONE",
    # Support tickets
    "SUPPORT_TICKET_OPENED",
    "SUPPORT_TICKET_REPLIED",
    "SUPPORT_TICKET_STATUS_CHANGED",
    # Compliance issues
    "COMPLIANCE_ISSUE_CREATED",
    "COMPLIANCE_ISSUE_UPDATED",
    "COMPLIANCE_ISSUE_RESOLVED",
    "COMPLIANCE_ISSUE_DISMISSED",
]

# --------------------------------------------------------------------------
# notification_type values (Python: class NotificationType(StrEnum))
# --------------------------------------------------------------------------
_NOTIFICATION_TYPE_VALUES = [
    "APPOINTMENT_CANCELLED",
    "APPOINTMENT_READY",
    "VISIT_STARTED",
    "VISIT_ENDED",
    "VISIT_SUBMITTED_FOR_SIGNATURE",
    "VISIT_SIGNED",
    "VISIT_COMPLETED",
    "BILLING_CONFIRMED",
    "SUPPORT_TICKET_OPENED",
    "SUPPORT_TICKET_REPLIED",
]


def upgrade() -> None:
    for value in _AUDIT_ACTION_VALUES:
        op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{value}'")
    for value in _NOTIFICATION_TYPE_VALUES:
        op.execute(f"ALTER TYPE notification_type ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # `ALTER TYPE ... DROP VALUE` was added in Postgres 16 and the
    # dev DB is on a Supabase pooler that runs an older version.
    # We don't drop values on downgrade — removing an enum value
    # that existing rows reference would break reads. Keep them in
    # place; rollback just no-ops.
    pass