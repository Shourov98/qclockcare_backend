"""Map Python StrEnums to their Postgres ENUM type names.

Alembic's `op.create_type` and `Enum(MyEnum, name="...")` need the type name
explicitly. This file is the single source of truth for those mappings.

If you add a new enum to `enums.py`, add it here too.
"""

from __future__ import annotations

from src.shared.domain.enums import (
    AgencyStatus,
    AgencySubscriptionPlan,
    AppointmentStatus,
    AuditAction,
    AuthAuditEventType,
    DocumentStatus,
    DocumentType,
    LicenseStatus,
    NotificationChannel,
    NotificationStatus,
    NotificationType,
    ProgramType,
    QualificationStatus,
    QualificationType,
    RelationshipType,
    ServiceItemStatus,
    ServiceType,
    TicketCommentKind,
    TicketPriority,
    TicketStatus,
    UserRole,
    UserStatus,
    VisitStatus,
)

# Python enum class -> Postgres ENUM name
ENUM_TYPE_NAMES: dict[type, str] = {
    AgencyStatus: "agency_status",
    AgencySubscriptionPlan: "agency_subscription_plan",
    AppointmentStatus: "appointment_status",
    AuditAction: "audit_action",
    AuthAuditEventType: "auth_audit_event_type",
    DocumentStatus: "document_status",
    DocumentType: "document_type",
    LicenseStatus: "license_status",
    NotificationChannel: "notification_channel",
    NotificationStatus: "notification_status",
    NotificationType: "notification_type",
    ProgramType: "program_type",
    QualificationStatus: "qualification_status",
    QualificationType: "qualification_type",
    RelationshipType: "relationship_type",
    ServiceItemStatus: "service_item_status",
    ServiceType: "service_type",
    TicketCommentKind: "ticket_comment_kind",
    TicketPriority: "ticket_priority",
    TicketStatus: "ticket_status",
    UserRole: "user_role",
    UserStatus: "user_status",
    VisitStatus: "visit_status",
}


def pg_name(python_enum: type) -> str:
    """Return the Postgres ENUM name for a given Python StrEnum class."""
    try:
        return ENUM_TYPE_NAMES[python_enum]
    except KeyError as exc:
        raise KeyError(
            f"Missing ENUM_TYPE_NAMES entry for {python_enum.__name__}; "
            f"add it to src/shared/domain/enum_mapping.py",
        ) from exc


__all__ = ["ENUM_TYPE_NAMES", "pg_name"]
