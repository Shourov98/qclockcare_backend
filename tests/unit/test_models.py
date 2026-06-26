"""ORM model smoke tests — verify all models can be imported and registered.

These don't hit a real DB; they just check that metadata is registered and
that the relationships resolve without import-time errors.
"""

from __future__ import annotations


def test_enums_load() -> None:
    from src.shared.domain.enums import (
        AgencyStatus,
        AppointmentStatus,
        AuditAction,
        AuthAuditEventType,
        ConfirmationStatus,
        DisputeReasonCode,
        NotificationChannel,
        NotificationStatus,
        NotificationType,
        ProgramType,
        QualificationStatus,
        QualificationType,
        RelationshipType,
        ServiceItemStatus,
        ServiceType,
        UserRole,
        UserStatus,
        VerificationStatus,
        VisitStatus,
    )

    # Every enum has at least one member
    for cls in (
        AgencyStatus,
        AppointmentStatus,
        AuditAction,
        AuthAuditEventType,
        ConfirmationStatus,
        DisputeReasonCode,
        NotificationChannel,
        NotificationStatus,
        NotificationType,
        ProgramType,
        QualificationStatus,
        QualificationType,
        RelationshipType,
        ServiceItemStatus,
        ServiceType,
        UserRole,
        UserStatus,
        VerificationStatus,
        VisitStatus,
    ):
        assert len(list(cls)) > 0, f"{cls.__name__} is empty"


def test_enum_mapping_complete() -> None:
    from src.shared.domain.enum_mapping import ENUM_TYPE_NAMES, pg_name
    from src.shared.domain.enums import (
        AgencyStatus,
        AppointmentStatus,
        AuditAction,
        AuthAuditEventType,
        ConfirmationStatus,
        DisputeReasonCode,
        NotificationChannel,
        NotificationStatus,
        NotificationType,
        ProgramType,
        QualificationStatus,
        QualificationType,
        RelationshipType,
        ServiceItemStatus,
        ServiceType,
        UserRole,
        UserStatus,
        VerificationStatus,
        VisitStatus,
    )

    for cls in (
        AgencyStatus,
        AppointmentStatus,
        AuditAction,
        AuthAuditEventType,
        ConfirmationStatus,
        DisputeReasonCode,
        NotificationChannel,
        NotificationStatus,
        NotificationType,
        ProgramType,
        QualificationStatus,
        QualificationType,
        RelationshipType,
        ServiceItemStatus,
        ServiceType,
        UserRole,
        UserStatus,
        VerificationStatus,
        VisitStatus,
    ):
        # Every enum has a Postgres name mapping
        assert cls in ENUM_TYPE_NAMES, f"{cls.__name__} not in ENUM_TYPE_NAMES"
        # Postgres name is snake_case
        name = pg_name(cls)
        assert name.islower(), f"{cls.__name__} maps to {name!r}, not lowercase"
        assert "_" in name or len(name) < 8, f"{name!r} doesn't look like a pg type name"


def test_identity_models_register() -> None:
    from src.modules.identity.models import (  # noqa: F401 — import to register
        AuthAuditEvent,
        EmailVerificationOtp,
        User,
        UserRoleAssignment,
    )
    from src.shared.domain.base_entity import Base

    metadata = Base.metadata
    assert "users" in metadata.tables
    assert "user_roles" in metadata.tables
    assert "email_verification_otps" in metadata.tables
    assert "auth_audit_events" in metadata.tables


def test_agency_models_register() -> None:
    from src.modules.agencies.models import (  # noqa: F401
        Agency,
        AgencyProgram,
        Program,
    )
    from src.shared.domain.base_entity import Base

    metadata = Base.metadata
    assert "agencies" in metadata.tables
    assert "programs" in metadata.tables
    assert "agency_programs" in metadata.tables


def test_user_table_has_expected_columns() -> None:
    from src.modules.identity.models import User
    from src.shared.domain.base_entity import Base

    cols = {c.name for c in Base.metadata.tables[User.__tablename__].columns}
    expected = {
        "id",
        "email",
        "password_hash",
        "full_name",
        "phone",
        "status",
        "failed_login_attempts",
        "locked_until",
        "last_login_at",
        "last_password_change_at",
        "must_change_password",
        "email_verified_at",
        "invitation_token_hash",
        "invitation_token_expires_at",
        "invitation_consumed_at",
        "created_at",
        "updated_at",
        "deleted_at",
    }
    assert expected.issubset(cols), f"Missing columns: {expected - cols}"


def test_agency_table_has_expected_columns() -> None:
    from src.modules.agencies.models import Agency
    from src.shared.domain.base_entity import Base

    cols = {c.name for c in Base.metadata.tables[Agency.__tablename__].columns}
    expected = {
        "id",
        "name",
        "status",
        "timezone",
        "settings",
        "created_at",
        "updated_at",
        "deleted_at",
    }
    assert expected.issubset(cols), f"Missing columns: {expected - cols}"
