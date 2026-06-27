"""Postgres ENUM types — Python-side mirror.

Every Postgres enum declared in `13_DATABASE_SCHEMA_COMPLETE.md` has a
matching `StrEnum` here. These are used by:
    - SQLAlchemy column definitions (`mapped_column(..., Enum(MyEnum))`)
    - Pydantic schemas (so API responses validate against the same values)
    - Service / repository code (to avoid stringly-typed comparisons)

Enum values MUST match the Postgres ENUM definitions exactly (case-sensitive).
If you change one side, change both. Migrations handle the DB side; this file
is the source of truth for the Python side.

Naming convention: `UserRole`, `UserStatus`, `ProgramType`, etc. — singular,
PascalCase. Postgres: `user_role`, `user_status`, `program_type` (singular).
"""

from __future__ import annotations

from enum import StrEnum


# --------------------------------------------------------------------------
# User & roles
# --------------------------------------------------------------------------
class UserRole(StrEnum):
    """Roles a user can hold within an agency.

    SUPER_ADMIN has `agency_id = NULL` and can hold only SUPER_ADMIN.
    """

    SUPER_ADMIN = "SUPER_ADMIN"
    AGENCY_ADMIN = "AGENCY_ADMIN"
    STAFF = "STAFF"
    PATIENT = "PATIENT"
    GUARDIAN = "GUARDIAN"


class UserStatus(StrEnum):
    """Lifecycle status of a user account."""

    INVITED = "INVITED"  # user created, invitation email not yet accepted
    EMAIL_VERIFICATION_PENDING = "EMAIL_VERIFICATION_PENDING"  # password set, awaiting OTP verify
    ACTIVE = "ACTIVE"  # email verified, can log in
    INACTIVE = "INACTIVE"  # admin-deactivated
    LOCKED = "LOCKED"  # too many failed logins / OTP attempts
    ARCHIVED = "ARCHIVED"


# --------------------------------------------------------------------------
# Agency
# --------------------------------------------------------------------------
class AgencyStatus(StrEnum):
    ACTIVE = "ACTIVE"
    TRIAL = "TRIAL"
    SUSPENDED = "SUSPENDED"
    CHURNED = "CHURNED"


# --------------------------------------------------------------------------
# Programs
# --------------------------------------------------------------------------
class ProgramType(StrEnum):
    """Waiver / service program types."""

    PCA = "PCA"
    CFSS = "CFSS"
    D245 = "245D"
    ARMHS = "ARMHS"
    COUNSELING = "COUNSELING"


class ServiceType(StrEnum):
    """Specific service within a program."""

    PERSONAL_CARE = "PERSONAL_CARE"
    HOMEMAKING = "HOMEMAKING"
    RESPITE = "RESPITE"
    SKILLED_NURSING = "SKILLED_NURSING"
    MENTAL_HEALTH = "MENTAL_HEALTH"
    COUNSELING_INDIVIDUAL = "COUNSELING_INDIVIDUAL"
    COUNSELING_GROUP = "COUNSELING_GROUP"


# --------------------------------------------------------------------------
# Appointments
# --------------------------------------------------------------------------
class AppointmentStatus(StrEnum):
    DRAFT = "DRAFT"
    SCHEDULED = "SCHEDULED"
    NOTIFICATION_SENT = "NOTIFICATION_SENT"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    CONFIRMED = "CONFIRMED"
    RESCHEDULE_REQUESTED = "RESCHEDULE_REQUESTED"
    CANCELLATION_REQUESTED = "CANCELLATION_REQUESTED"
    ASSIGNED = "ASSIGNED"
    CHECKED_IN = "CHECKED_IN"
    IN_PROGRESS = "IN_PROGRESS"
    CHECKED_OUT = "CHECKED_OUT"
    COMPLETED = "COMPLETED"
    AWAITING_SERVICE_VERIFICATION = "AWAITING_SERVICE_VERIFICATION"
    SERVICE_VERIFIED = "SERVICE_VERIFIED"
    DISPUTED = "DISPUTED"
    UNDER_REVIEW = "UNDER_REVIEW"
    APPROVED_FOR_BILLING = "APPROVED_FOR_BILLING"
    PAID = "PAID"
    CANCELLED = "CANCELLED"
    NO_SHOW = "NO_SHOW"
    REJECTED = "REJECTED"


class ServiceItemStatus(StrEnum):
    PENDING = "PENDING"
    DONE = "DONE"
    NOT_DONE = "NOT_DONE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NEEDS_FOLLOW_UP = "NEEDS_FOLLOW_UP"


# --------------------------------------------------------------------------
# Appointment event log (immutable domain timeline)
# --------------------------------------------------------------------------
class AppointmentEventType(StrEnum):
    """Domain-level events appended to `appointment_events`.

    Distinct from `AuditAction` (security/compliance trail) — events
    capture *what happened* to the appointment in business terms,
    not who audited it. Stored as `text` on the row (rather than a
    Postgres enum) so new event types can ship without a migration.
    """

    STATUS_TRANSITION = "STATUS_TRANSITION"
    CONFIRMATION_FILED = "CONFIRMATION_FILED"
    RESCHEDULE_REQUESTED = "RESCHEDULE_REQUESTED"
    CANCELLATION_REQUESTED = "CANCELLATION_REQUESTED"
    CANCELLED_BY_ADMIN = "CANCELLED_BY_ADMIN"


# --------------------------------------------------------------------------
# Visits
# --------------------------------------------------------------------------
class VisitStatus(StrEnum):
    CHECKED_IN = "CHECKED_IN"
    IN_PROGRESS = "IN_PROGRESS"
    CHECKED_OUT = "CHECKED_OUT"
    COMPLETED = "COMPLETED"


# --------------------------------------------------------------------------
# Confirmations / verifications
# --------------------------------------------------------------------------
class ConfirmationStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    DECLINED = "DECLINED"


class VerificationStatus(StrEnum):
    VERIFIED = "VERIFIED"
    DISPUTED = "DISPUTED"


class DisputeReasonCode(StrEnum):
    STAFF_NEVER_ARRIVED = "STAFF_NEVER_ARRIVED"
    STAFF_ARRIVED_LATE = "STAFF_ARRIVED_LATE"
    STAFF_LEFT_EARLY = "STAFF_LEFT_EARLY"
    SERVICE_NOT_COMPLETED = "SERVICE_NOT_COMPLETED"
    WRONG_SERVICE_MARKED_DONE = "WRONG_SERVICE_MARKED_DONE"
    WRONG_NOTE = "WRONG_NOTE"
    POOR_SERVICE = "POOR_SERVICE"
    OTHER = "OTHER"


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------
class NotificationChannel(StrEnum):
    IN_APP = "IN_APP"
    EMAIL = "EMAIL"
    SMS = "SMS"
    PUSH = "PUSH"


class NotificationStatus(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    BOUNCED = "BOUNCED"
    READ = "READ"


class NotificationType(StrEnum):
    APPOINTMENT_CREATED = "APPOINTMENT_CREATED"
    APPOINTMENT_ASSIGNED = "APPOINTMENT_ASSIGNED"
    APPOINTMENT_CONFIRMED = "APPOINTMENT_CONFIRMED"
    APPOINTMENT_RESCHEDULE_REQUESTED = "APPOINTMENT_RESCHEDULE_REQUESTED"
    APPOINTMENT_CANCELLATION_REQUESTED = "APPOINTMENT_CANCELLATION_REQUESTED"
    APPOINTMENT_CANCELLED = "APPOINTMENT_CANCELLED"
    VISIT_CHECK_IN_REMINDER = "VISIT_CHECK_IN_REMINDER"
    VISIT_CHECKED_IN = "VISIT_CHECKED_IN"
    VISIT_CHECKED_OUT = "VISIT_CHECKED_OUT"
    SERVICE_VERIFIED = "SERVICE_VERIFIED"
    SERVICE_DISPUTED = "SERVICE_DISPUTED"
    STAFF_INVITATION = "STAFF_INVITATION"
    PASSWORD_RESET = "PASSWORD_RESET"
    GENERIC = "GENERIC"


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------
class AuditAction(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    STATUS_TRANSITION = "STATUS_TRANSITION"
    READ = "READ"
    LOGIN = "LOGIN"
    LOGOUT = "LOGOUT"
    LOGIN_FAILED = "LOGIN_FAILED"
    ROLE_GRANTED = "ROLE_GRANTED"
    ROLE_REVOKED = "ROLE_REVOKED"
    LINK_PATIENT_GUARDIAN = "LINK_PATIENT_GUARDIAN"
    UNLINK_PATIENT_GUARDIAN = "UNLINK_PATIENT_GUARDIAN"
    APPOINTMENT_CONFIRMED = "APPOINTMENT_CONFIRMED"
    APPOINTMENT_RESCHEDULE_REQUESTED = "APPOINTMENT_RESCHEDULE_REQUESTED"
    APPOINTMENT_CANCELLATION_REQUESTED = "APPOINTMENT_CANCELLATION_REQUESTED"
    APPOINTMENT_CANCELLED = "APPOINTMENT_CANCELLED"
    APPOINTMENT_ASSIGNED = "APPOINTMENT_ASSIGNED"
    VISIT_CHECKED_IN = "VISIT_CHECKED_IN"
    VISIT_CHECKED_OUT = "VISIT_CHECKED_OUT"
    SERVICE_VERIFIED = "SERVICE_VERIFIED"
    SERVICE_DISPUTED = "SERVICE_DISPUTED"


class AuthAuditEventType(StrEnum):
    """Events specific to authentication flows (ADR-0016)."""

    INVITATION_SENT = "INVITATION_SENT"
    INVITATION_ACCEPTED = "INVITATION_ACCEPTED"
    INVITATION_EXPIRED = "INVITATION_EXPIRED"
    PASSWORD_SET = "PASSWORD_SET"
    OTP_SENT = "OTP_SENT"
    OTP_RESENT = "OTP_RESENT"
    OTP_VERIFIED = "OTP_VERIFIED"
    OTP_FAILED = "OTP_FAILED"
    OTP_LOCKED = "OTP_LOCKED"
    OTP_EXPIRED = "OTP_EXPIRED"
    EMAIL_VERIFIED = "EMAIL_VERIFIED"
    LOGIN_SUCCESS = "LOGIN_SUCCESS"
    LOGIN_FAILED = "LOGIN_FAILED"
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"
    ACCOUNT_UNLOCKED = "ACCOUNT_UNLOCKED"
    PASSWORD_CHANGED = "PASSWORD_CHANGED"
    PASSWORD_RESET_REQUESTED = "PASSWORD_RESET_REQUESTED"
    PASSWORD_RESET_COMPLETED = "PASSWORD_RESET_COMPLETED"
    TOKEN_REFRESHED = "TOKEN_REFRESHED"
    TOKEN_REVOKED = "TOKEN_REVOKED"


# --------------------------------------------------------------------------
# Relationships
# --------------------------------------------------------------------------
class RelationshipType(StrEnum):
    SELF = "SELF"
    SPOUSE = "SPOUSE"
    PARENT = "PARENT"
    CHILD = "CHILD"
    SON = "SON"
    DAUGHTER = "DAUGHTER"
    SIBLING = "SIBLING"
    GRANDPARENT = "GRANDPARENT"
    GRANDCHILD = "GRANDCHILD"
    FRIEND = "FRIEND"
    GUARDIAN = "GUARDIAN"
    CONSERVATOR = "CONSERVATOR"
    CASEWORKER = "CASEWORKER"
    POWER_OF_ATTORNEY = "POWER_OF_ATTORNEY"
    OTHER = "OTHER"


# --------------------------------------------------------------------------
# Staff qualifications
# --------------------------------------------------------------------------
class QualificationType(StrEnum):
    PCA_CERTIFIED = "PCA_CERTIFIED"
    CFSS_TRAINED = "CFSS_TRAINED"
    RN = "RN"
    LPN = "LPN"
    CNA = "CNA"
    ARMHS_PROVIDER = "ARMHS_PROVIDER"
    COUNSELOR_LICENSED = "COUNSELOR_LICENSED"
    FIRST_AID = "FIRST_AID"
    CPR = "CPR"
    BACKGROUND_CHECK = "BACKGROUND_CHECK"
    OTHER = "OTHER"


class QualificationStatus(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    PENDING_VERIFICATION = "PENDING_VERIFICATION"
    REVOKED = "REVOKED"


__all__ = [
    "AgencyStatus",
    "AppointmentEventType",
    "AppointmentStatus",
    "AuditAction",
    "AuthAuditEventType",
    "ConfirmationStatus",
    "DisputeReasonCode",
    "NotificationChannel",
    "NotificationStatus",
    "NotificationType",
    "ProgramType",
    "QualificationStatus",
    "QualificationType",
    "RelationshipType",
    "ServiceItemStatus",
    "ServiceType",
    "UserRole",
    "UserStatus",
    "VerificationStatus",
    "VisitStatus",
]
