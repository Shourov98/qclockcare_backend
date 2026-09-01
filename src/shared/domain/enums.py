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

    SUPER_ADMIN has `agency_id = NULL` and has full cross-tenant access.
    PLATFORM_ADMIN has `agency_id = NULL` and holds one or more scopes
        from `AdminScope` for granular cross-tenant access.
    AGENCY_ADMIN is scoped to a single agency.
    """

    SUPER_ADMIN = "SUPER_ADMIN"
    PLATFORM_ADMIN = "PLATFORM_ADMIN"
    AGENCY_ADMIN = "AGENCY_ADMIN"
    STAFF = "STAFF"
    PATIENT = "PATIENT"
    GUARDIAN = "GUARDIAN"


class AdminScope(StrEnum):
    """Scopes a PLATFORM_ADMIN user can hold.

    Scopes are checked at the router level via `require_scope(scope)`.
    SUPER_ADMIN users always pass scope checks without needing rows
    in the `admin_scopes` table.
    """

    AGENCIES = "AGENCIES"   # read + patch all agencies (status, plan)
    CLINICAL = "CLINICAL"   # read patients/staff across tenants
    SUPPORT = "SUPPORT"     # read audit logs, cross-tenant notifications


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


class AgencySubscriptionPlan(StrEnum):
    BASIC = "BASIC"
    PROFESSIONAL = "PROFESSIONAL"
    ENTERPRISE = "ENTERPRISE"


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
    """5-state lifecycle per the canonical spec (see
    `QlockCare_appointemnt_flow.md`). The appointment moves through:

        SCHEDULED -> READY -> IN_PROGRESS -> AWAITING_SIGNATURE -> COMPLETED

    Plus exception edges: `CANCELLED`, `MISSED`, `REJECTED` from any
    pre-visit state. Service-verification / dispute / billing statuses
    are no longer first-class appointment states — those concepts moved
    to `AppointmentSignature` and (future) the billing module.

    Postgres ENUM `appointment_status` MUST match these values exactly.
    """

    SCHEDULED = "SCHEDULED"
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    AWAITING_SIGNATURE = "AWAITING_SIGNATURE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    MISSED = "MISSED"
    REJECTED = "REJECTED"


# Statuses the UI treats as "active / in-flight" (i.e. not yet finalized
# or exceptioned). Used as a default filter for the appointment list.
APPOINTMENT_ACTIVE_STATUSES: frozenset[AppointmentStatus] = frozenset(
    {
        AppointmentStatus.SCHEDULED,
        AppointmentStatus.READY,
        AppointmentStatus.IN_PROGRESS,
        AppointmentStatus.AWAITING_SIGNATURE,
    }
)


class ServiceItemStatus(StrEnum):
    """Lifecycle of an `AppointmentActivity` row (per-appointment checklist).

    Same five states as before — the rename to "Activity" doesn't change
    the per-row state machine. `DONE` is required on every row before
    the caregiver can submit "End Task"; `NOT_DONE` requires a reason
    (enforced at the service layer).
    """

    PENDING = "PENDING"
    DONE = "DONE"
    NOT_DONE = "NOT_DONE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NEEDS_FOLLOW_UP = "NEEDS_FOLLOW_UP"


# --------------------------------------------------------------------------
# Visits — mirrors the appointment lifecycle
# --------------------------------------------------------------------------
class VisitStatus(StrEnum):
    """The materialized-visit row follows the same 8-state lifecycle
    as `AppointmentStatus`. The visit row is created when the staff
    app POSTs `/visits` (transition `READY -> IN_PROGRESS`) and walks
    the same path to `COMPLETED`.

    The migration collapses the old `CHECKED_IN / CHECKED_OUT` middle
    states into `IN_PROGRESS` per the spec.
    """

    SCHEDULED = "SCHEDULED"
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    AWAITING_SIGNATURE = "AWAITING_SIGNATURE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    MISSED = "MISSED"
    REJECTED = "REJECTED"


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
    APPOINTMENT_CANCELLED = "APPOINTMENT_CANCELLED"
    APPOINTMENT_READY = "APPOINTMENT_READY"
    VISIT_STARTED = "VISIT_STARTED"
    VISIT_ENDED = "VISIT_ENDED"
    VISIT_SUBMITTED_FOR_SIGNATURE = "VISIT_SUBMITTED_FOR_SIGNATURE"
    VISIT_SIGNED = "VISIT_SIGNED"
    VISIT_COMPLETED = "VISIT_COMPLETED"
    BILLING_CONFIRMED = "BILLING_CONFIRMED"
    STAFF_INVITATION = "STAFF_INVITATION"
    PASSWORD_RESET = "PASSWORD_RESET"
    SUPPORT_TICKET_OPENED = "SUPPORT_TICKET_OPENED"
    SUPPORT_TICKET_REPLIED = "SUPPORT_TICKET_REPLIED"
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
    APPOINTMENT_CREATED = "APPOINTMENT_CREATED"
    APPOINTMENT_CANCELLED = "APPOINTMENT_CANCELLED"
    APPOINTMENT_MARKED_READY = "APPOINTMENT_MARKED_READY"
    APPOINTMENT_ASSIGNED = "APPOINTMENT_ASSIGNED"
    VISIT_STARTED = "VISIT_STARTED"
    VISIT_SUBMITTED_FOR_SIGNATURE = "VISIT_SUBMITTED_FOR_SIGNATURE"
    VISIT_SIGNED = "VISIT_SIGNED"
    VISIT_COMPLETED = "VISIT_COMPLETED"
    BILLING_CONFIRMED = "BILLING_CONFIRMED"
    ACTIVITY_MARKED_DONE = "ACTIVITY_MARKED_DONE"
    ACTIVITY_MARKED_NOT_DONE = "ACTIVITY_MARKED_NOT_DONE"
    SUPPORT_TICKET_OPENED = "SUPPORT_TICKET_OPENED"
    SUPPORT_TICKET_REPLIED = "SUPPORT_TICKET_REPLIED"
    SUPPORT_TICKET_STATUS_CHANGED = "SUPPORT_TICKET_STATUS_CHANGED"
    COMPLIANCE_ISSUE_CREATED = "COMPLIANCE_ISSUE_CREATED"
    COMPLIANCE_ISSUE_UPDATED = "COMPLIANCE_ISSUE_UPDATED"
    COMPLIANCE_ISSUE_RESOLVED = "COMPLIANCE_ISSUE_RESOLVED"
    COMPLIANCE_ISSUE_DISMISSED = "COMPLIANCE_ISSUE_DISMISSED"


# --------------------------------------------------------------------------
# Support tickets (admin dashboard `/support`)
# --------------------------------------------------------------------------
class TicketStatus(StrEnum):
    """Lifecycle of an internal admin support ticket.

    Status transitions are unrestricted on the backend (the UI hides
    illegal transitions) — we keep it simple because the volume is
    low and over-engineering the state machine hasn't been worth it.
    """

    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    PENDING = "PENDING"          # waiting on someone outside the team
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class TicketPriority(StrEnum):
    """Used by the dashboard to colour-code + sort the table."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class TicketCommentKind(StrEnum):
    """What kind of timeline entry a ticket comment represents.

    Lives in `enums.py` (rather than `tickets/models.py`) so the Postgres
    ENUM mapping in `enum_mapping.py` can register it alongside the other
    cross-cutting StrEnums.

    `COMMENT` — a regular reply.
    `STATUS_CHANGE` — auto-logged when ticket.status transitions.
    `ASSIGNMENT` — auto-logged when ticket.assignee_user_id changes.
    `ATTACHMENT` — placeholder for future file-upload support.
    """

    COMMENT = "COMMENT"
    STATUS_CHANGE = "STATUS_CHANGE"
    ASSIGNMENT = "ASSIGNMENT"
    ATTACHMENT = "ATTACHMENT"


# --------------------------------------------------------------------------
# Compliance — agency documents & licenses
# --------------------------------------------------------------------------
class DocumentType(StrEnum):
    """Top-level classification of a per-agency required document."""

    LICENSE = "LICENSE"            # operating / state license
    CERTIFICATE = "CERTIFICATE"    # HIPAA / OSHA / CPR etc.
    DOCUMENT = "DOCUMENT"          # catch-all (insurance, audit, manual)
    PERMIT = "PERMIT"              # facility / construction / occupancy
    POLICY = "POLICY"              # handbook / privacy / safety plan
    REPORT = "REPORT"              # annual audit / financial report


class DocumentStatus(StrEnum):
    """Lifecycle of an `agency_document` row.

    `MISSING`  — required by policy but not yet uploaded.
    `PENDING`  — uploaded; awaiting admin review.
    `VALID`    — uploaded + verified; not yet near expiry.
    `EXPIRING` — within `EXPIRING_SOON_DAYS` of `expires_at`.
    `EXPIRED`  — past `expires_at`.
    `REJECTED` — uploaded but rejected (e.g. wrong document type).
    """

    MISSING = "MISSING"
    PENDING = "PENDING"
    VALID = "VALID"
    EXPIRING = "EXPIRING"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


class LicenseStatus(StrEnum):
    """Derived status for `agency_license` rows based on `expires_at`.

    Mirrors `DocumentStatus` for the licence subset. The FE shows these
    as Critical / Warning / Upcoming buckets in `ExpiringLicensesTable`.
    """

    VALID = "VALID"        # > 60 days until expiry
    UPCOMING = "UPCOMING"  # 30–60 days
    WARNING = "WARNING"    # 14–30 days
    CRITICAL = "CRITICAL"  # < 14 days OR past expiry
    EXPIRED = "EXPIRED"    # past expiry


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


# --------------------------------------------------------------------------
# Patient / Guardian → AGENCY_ADMIN support tickets (`/portal/support` +
# `/agency/support` surfaces). Distinct from the internal admin
# `TicketStatus` enum used by `/admin/tickets` — the public surface
# has only four states and uses different transitions.
# --------------------------------------------------------------------------
class SupportTicketStatus(StrEnum):
    """Lifecycle of a help/support ticket opened by a patient or guardian.

    OPEN            — new, AGENCY_ADMIN has not replied yet.
    AWAITING_REPLY  — patient/guardian replied, AGENCY_ADMIN owes a response.
    RESOLVED        — AGENCY_ADMIN marked the issue fixed (sets `resolved_at`).
    CLOSED          — terminal state; thread is read-only (sets `closed_at`).
    """

    OPEN = "OPEN"
    AWAITING_REPLY = "AWAITING_REPLY"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class SupportTicketPriority(StrEnum):
    """How urgent the issue is. Drives inbox sorting + colour-coding."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    URGENT = "URGENT"


class SupportTicketAuthorKind(StrEnum):
    """Which side wrote a given message (or opened the ticket).

    PATIENT       — patient themselves.
    GUARDIAN      — a legal guardian of the patient.
    AGENCY_ADMIN  — an admin at the agency answering the ticket.
    """

    PATIENT = "PATIENT"
    GUARDIAN = "GUARDIAN"
    AGENCY_ADMIN = "AGENCY_ADMIN"


# --------------------------------------------------------------------------
# Compliance issue queue (`/admin/compliance/issues`)
# --------------------------------------------------------------------------
class ComplianceIssueSeverity(StrEnum):
    """How serious the issue is. Drives the FE colour-coded badge + sort.

    Matches the values the admin FE renders today in
    `ComplianceIssueQueueTable` (Critical / High / Medium / Low).
    """

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ComplianceIssueStatus(StrEnum):
    """Lifecycle of an issue in the admin queue.

    OPEN            — newly filed; no work has started.
    IN_PROGRESS     — assigned; being worked.
    PENDING_REVIEW  — work submitted; awaiting admin review.
    RESOLVED        — closed as fixed (stamps `resolved_at`).
    DISMISSED       — closed without action (stamps `resolved_at` too).
    """

    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    PENDING_REVIEW = "PENDING_REVIEW"
    RESOLVED = "RESOLVED"
    DISMISSED = "DISMISSED"


class ComplianceIssueCategory(StrEnum):
    """What kind of compliance gap this issue represents.

    Used to group counts in the admin dashboard + drive the FE category
    badge in `ComplianceIssueQueueTable`. Service Authorizations is
    in the enum for forward-compat — the table will be added later.
    """

    DOCUMENTATION = "DOCUMENTATION"
    STAFF_CREDENTIAL = "STAFF_CREDENTIAL"
    SAFETY = "SAFETY"
    SERVICE_AUTH = "SERVICE_AUTH"
    STAFF_TRAINING = "STAFF_TRAINING"
    OTHER = "OTHER"


__all__ = [
    "APPOINTMENT_ACTIVE_STATUSES",
    "AgencyStatus",
    "AppointmentStatus",
    "AuditAction",
    "AuthAuditEventType",
    "ComplianceIssueCategory",
    "ComplianceIssueSeverity",
    "ComplianceIssueStatus",
    "DocumentStatus",
    "DocumentType",
    "LicenseStatus",
    "NotificationChannel",
    "NotificationStatus",
    "NotificationType",
    "ProgramType",
    "QualificationStatus",
    "QualificationType",
    "RelationshipType",
    "ServiceItemStatus",
    "ServiceType",
    "SupportTicketAuthorKind",
    "SupportTicketPriority",
    "SupportTicketStatus",
    "TicketCommentKind",
    "TicketPriority",
    "TicketStatus",
    "UserRole",
    "UserStatus",
    "VisitStatus",
]
