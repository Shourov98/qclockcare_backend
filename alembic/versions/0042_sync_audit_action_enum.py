"""Synchronize the audit_action Postgres enum with the application enum.

Revision ID: 0042_sync_audit_action_enum
Revises: 0041_drop_legacy_activity_service_type
"""

from __future__ import annotations

from alembic import op


revision = "0042_sync_audit_action_enum"
down_revision = "0041_drop_legacy_activity_service_type"
branch_labels = None
depends_on = None


_AUDIT_ACTION_VALUES = (
    "CREATE",
    "UPDATE",
    "DELETE",
    "STATUS_TRANSITION",
    "READ",
    "LOGIN",
    "LOGOUT",
    "LOGIN_FAILED",
    "ROLE_GRANTED",
    "ROLE_REVOKED",
    "LINK_PATIENT_GUARDIAN",
    "UNLINK_PATIENT_GUARDIAN",
    "APPOINTMENT_CREATED",
    "APPOINTMENT_CANCELLED",
    "APPOINTMENT_MARKED_READY",
    "APPOINTMENT_ASSIGNED",
    "VISIT_STARTED",
    "VISIT_SUBMITTED_FOR_SIGNATURE",
    "VISIT_SIGNED",
    "VISIT_COMPLETED",
    "BILLING_CONFIRMED",
    "ACTIVITY_MARKED_DONE",
    "ACTIVITY_MARKED_NOT_DONE",
    "SUPPORT_TICKET_OPENED",
    "SUPPORT_TICKET_REPLIED",
    "SUPPORT_TICKET_STATUS_CHANGED",
    "COMPLIANCE_ISSUE_CREATED",
    "COMPLIANCE_ISSUE_UPDATED",
    "COMPLIANCE_ISSUE_RESOLVED",
    "COMPLIANCE_ISSUE_DISMISSED",
)


def upgrade() -> None:
    for value in _AUDIT_ACTION_VALUES:
        op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # Enum values cannot be removed safely while audit rows may reference
    # them, so downgrade intentionally leaves the expanded enum in place.
    pass
