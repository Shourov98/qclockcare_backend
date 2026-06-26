"""Identity module — authentication-related ORM models.

Tables:
- `users`                     — auth identity (email, password, status)
- `user_roles`                — per-agency role assignments
- `email_verification_otps`   — issued OTPs (hashed)
- `auth_audit_events`         — auth-specific event log (ADR-0016)

See `13_DATABASE_SCHEMA_COMPLETE.md` §4 + `25_AUTH_AND_HOSTING_DECISIONS.md` §7.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.domain.base_entity import (
    Base,
    IdMixin,
    SoftDeleteMixin,
    TimestampedMixin,
)
from src.shared.domain.enum_mapping import pg_name
from src.shared.domain.enums import AuthAuditEventType, UserRole, UserStatus

if TYPE_CHECKING:
    from src.modules.agencies.models import Agency
    from src.modules.staff.models import StaffProfile
    from src.modules.patients.models import (
        GuardianProfile,
        PatientProfile,
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _utcnow() -> datetime:
    """UTC now, timezone-aware. Local import to avoid circular issues."""

    return datetime.now(tz=UTC)


# --------------------------------------------------------------------------
# users
# --------------------------------------------------------------------------
class User(IdMixin, TimestampedMixin, SoftDeleteMixin, Base):
    """Authentication identity and core profile.

    One row per login. `email` is citext (case-insensitive, unique). `password_hash`
    is null until the invitation is accepted. Status starts at `INVITED` →
    `EMAIL_VERIFICATION_PENDING` after `/auth/accept-invitation` → `ACTIVE` on
    successful OTP verification.
    """

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(CITEXT(), unique=True, nullable=False)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, name=pg_name(UserStatus)),
        nullable=False,
        default=UserStatus.INVITED,
        server_default=UserStatus.INVITED.value,
    )

    # Auth state
    failed_login_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_password_change_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # Email verification (ADR-0016)
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    invitation_token_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    invitation_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    invitation_consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    roles: Mapped[list[UserRoleAssignment]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    otps: Mapped[list[EmailVerificationOtp]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    auth_events: Mapped[list[AuthAuditEvent]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    staff_profiles: Mapped[list["StaffProfile"]] = relationship(  # noqa: F821
        "StaffProfile", back_populates="user", cascade="all, delete-orphan"
    )
    patient_profiles: Mapped[list["PatientProfile"]] = relationship(  # noqa: F821
        "PatientProfile", back_populates="user", cascade="all, delete-orphan"
    )
    guardian_profiles: Mapped[list["GuardianProfile"]] = relationship(  # noqa: F821
        "GuardianProfile", back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index(
            "idx_users_status",
            "status",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "idx_users_deleted_at",
            "deleted_at",
            postgresql_where=text("deleted_at IS NOT NULL"),
        ),
        Index(
            "idx_users_email_verified",
            "email_verified_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


# --------------------------------------------------------------------------
# user_roles
# --------------------------------------------------------------------------
class UserRoleAssignment(IdMixin, TimestampedMixin, Base):
    """A user holding a role within (or across) an agency.

    SUPER_ADMIN must have `agency_id = NULL`; all other roles must have an
    `agency_id`. Enforced by the `ck_super_admin_no_agency` check constraint.
    """

    __tablename__ = "user_roles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name=pg_name(UserRole)),
        nullable=False,
    )
    agency_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id", ondelete="CASCADE"),
        nullable=True,
    )

    user: Mapped[User] = relationship(back_populates="roles")
    agency: Mapped["Agency | None"] = relationship(  # noqa: F821
        "Agency", back_populates="user_roles"
    )

    __table_args__ = (
        CheckConstraint(
            "(role = 'SUPER_ADMIN' AND agency_id IS NULL) OR (role <> 'SUPER_ADMIN')",
            name="ck_super_admin_no_agency",
        ),
        Index("idx_user_roles_user_id", "user_id"),
        Index(
            "idx_user_roles_agency_id",
            "agency_id",
            postgresql_where=text("agency_id IS NOT NULL"),
        ),
        # Unique: a user can hold the same role ONCE per agency.
        # For SUPER_ADMIN, agency_id is NULL — Postgres treats NULLs as
        # distinct in unique constraints. The CHECK constraint above
        # allows at most one SUPER_ADMIN row per user; we enforce that
        # at the service layer.
        Index(
            "uq_user_role_per_agency",
            "user_id",
            "role",
            "agency_id",
            unique=True,
        ),
    )


# --------------------------------------------------------------------------
# email_verification_otps
# --------------------------------------------------------------------------
class EmailVerificationOtp(IdMixin, Base):
    """One row per issued OTP (ADR-0016).

    Only one *active* (unconsumed, unexpired) row per user at a time.
    Re-sending consumes the previous one. The plaintext OTP is **never**
    stored and never logged — only the argon2 hash is persisted.
    """

    __tablename__ = "email_verification_otps"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    email: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    otp_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET(), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    # `created_at` only (no `updated_at`) — rows are immutable once consumed.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default="now()",
    )

    user: Mapped[User] = relationship(back_populates="otps")

    __table_args__ = (
        Index(
            "idx_otp_user_active",
            "user_id",
            "expires_at",
            postgresql_where=text("consumed_at IS NULL"),
        ),
        Index("idx_otp_email_recent", "email", "created_at"),
    )


# --------------------------------------------------------------------------
# auth_audit_events
# --------------------------------------------------------------------------
class AuthAuditEvent(IdMixin, Base):
    """Immutable log of authentication-related events (ADR-0016).

    Append-only at the application layer; trigger-enforced at the DB layer
    (see migration). `event_metadata` carries event-specific context (request
    IP, user agent, OTP delivery result, etc.).
    """

    __tablename__ = "auth_audit_events"

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_type: Mapped[AuthAuditEventType] = mapped_column(
        Enum(AuthAuditEventType, name=pg_name(AuthAuditEventType)),
        nullable=False,
    )
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, default=dict, server_default="{}"
    )
    ip_address: Mapped[str | None] = mapped_column(INET(), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default="now()",
    )

    user: Mapped[User | None] = relationship(back_populates="auth_events")

    __table_args__ = (
        Index("idx_auth_audit_user", "user_id", "created_at"),
        Index("idx_auth_audit_type_recent", "event_type", "created_at"),
    )


# --------------------------------------------------------------------------
# refresh_tokens
# --------------------------------------------------------------------------
class RefreshToken(Base):
    """One row per issued refresh JWT (ADR-0016 §7.2).

    `jti` is the JWT ID claim — unique, primary key. Revocation is
    idempotent (`revoked_at` set once). Expired rows stay around for
    forensics; the lookup query filters on `revoked_at IS NULL AND
    expires_at > now()`.
    """

    __tablename__ = "refresh_tokens"

    jti: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default="now()",
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET(), nullable=True)

    user: Mapped[User] = relationship()


# --------------------------------------------------------------------------
# single_use_tokens
# --------------------------------------------------------------------------
class SingleUseToken(Base):
    """One row per issued invitation / password-reset token (ADR-0016 §7.3).

    `purpose` is constrained to `'invitation' | 'password_reset'` at the DB
    level (`ck_single_use_tokens_purpose`). `consumed_at` is set on first
    valid use; `revoked_at` is set when we invalidate before consumption
    (e.g. another token of the same purpose has been used).
    """

    __tablename__ = "single_use_tokens"

    jti: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default="now()",
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[User] = relationship()


__all__ = [
    "AuthAuditEvent",
    "EmailVerificationOtp",
    "RefreshToken",
    "SingleUseToken",
    "User",
    "UserRoleAssignment",
]
