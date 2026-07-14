"""Staff module — ORM models for staff profiles, qualifications, availability.

Tables:
- `staff_profiles`         — per-agency staff record (links to users)
- `staff_qualifications`   — credentials held by a staff member
- `staff_availability`     — recurring weekly windows + one-off blocks

All three are agency-scoped; RLS policies are defined in migration 0004.

Lifecycle notes:
- `staff_profiles.status` reuses the `user_status` enum (INVITED → ACTIVE →
  INACTIVE/ARCHIVED) so staff and user accounts move through the same states.
- `staff_qualifications.status` uses `qualification_status` (PENDING_VERIFICATION
  → ACTIVE → EXPIRED/REVOKED) — documents may be verified out-of-band.
- `staff_availability` has no `updated_at` — rows are immutable: add a new
  block to override, never edit history.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    Time,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.domain.base_entity import Base, IdMixin, TimestampedMixin
from src.shared.domain.enum_mapping import pg_name
from src.shared.domain.enums import (
    ProgramType,
    QualificationStatus,
    QualificationType,
    UserStatus,
)
from src.shared.utils.datetime_utils import utc_now

if TYPE_CHECKING:
    from src.modules.agencies.models import Agency
    from src.modules.appointments.models import Appointment
    from src.modules.identity.models import User


# --------------------------------------------------------------------------
# staff_profiles
# --------------------------------------------------------------------------
class StaffProfile(IdMixin, TimestampedMixin, Base):
    """A staff member's profile within a single agency.

    One `User` can hold staff profiles at multiple agencies (multi-agency
    contractors); the `(agency_id, user_id)` unique constraint prevents
    duplicate profiles. `staff_code` is a per-agency human-readable
    identifier (e.g. badge number).

    Soft-delete is **not** used here — we set `terminated_at` to mark
    historical records, and the service layer filters by `status = ACTIVE`
    when listing the active roster.
    """

    __tablename__ = "staff_profiles"

    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    staff_code: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, name=pg_name(UserStatus)),
        nullable=False,
        default=UserStatus.INVITED,
        server_default=UserStatus.INVITED.value,
    )
    hired_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    terminated_at: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Relationships
    agency: Mapped[Agency] = relationship(
        "Agency", back_populates="staff_profiles"
    )
    user: Mapped[User] = relationship(
        "User", back_populates="staff_profiles"
    )
    qualifications: Mapped[list[StaffQualification]] = relationship(
        back_populates="staff",
        cascade="all, delete-orphan",
    )
    availability: Mapped[list[StaffAvailability]] = relationship(
        back_populates="staff",
        cascade="all, delete-orphan",
    )
    appointments: Mapped[list[Appointment]] = relationship(
        "Appointment", back_populates="staff"
    )

    __table_args__ = (
        # agency_id + user_id: a user can only have one profile per agency
        Index(
            "uq_staff_per_user_per_agency",
            "agency_id",
            "user_id",
            unique=True,
        ),
        # agency_id + staff_code: staff codes are scoped to the agency
        Index(
            "uq_staff_code_per_agency",
            "agency_id",
            "staff_code",
            unique=True,
        ),
        Index(
            "idx_staff_profiles_agency_id",
            "agency_id",
            postgresql_where=text("status <> 'ARCHIVED'"),
        ),
        Index("idx_staff_profiles_user_id", "user_id"),
        CheckConstraint(
            "(terminated_at IS NULL) OR (hired_at IS NULL) OR (terminated_at >= hired_at)",
            name="ck_staff_terminated_after_hired",
        ),
    )


# --------------------------------------------------------------------------
# staff_qualifications
# --------------------------------------------------------------------------
class StaffQualification(IdMixin, TimestampedMixin, Base):
    """A credential held by a staff member.

    `qualification_type` is the broad category (e.g. CPR, RN). The actual
    document (license PDF, certificate scan) is referenced by
    `document_storage_key` — the key in the S3/Supabase bucket, never a URL.

    `program_type` is optional: NULL means the qualification is universal
    across programs the agency offers. When set, it constrains the staff
    to that specific program (e.g. ARMHS_PROVIDER for ARMHS services only).
    """

    __tablename__ = "staff_qualifications"

    staff_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("staff_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id", ondelete="CASCADE"),
        nullable=False,
    )
    qualification_type: Mapped[QualificationType] = mapped_column(
        Enum(QualificationType, name=pg_name(QualificationType)),
        nullable=False,
    )
    # NULL = applies to all programs the agency offers
    program_type: Mapped[ProgramType | None] = mapped_column(
        Enum(ProgramType, name=pg_name(ProgramType)),
        nullable=True,
    )
    # S3/Supabase Storage key for the credential document
    document_storage_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    issued_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    expires_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[QualificationStatus] = mapped_column(
        Enum(QualificationStatus, name=pg_name(QualificationStatus)),
        nullable=False,
        default=QualificationStatus.PENDING_VERIFICATION,
        server_default=QualificationStatus.PENDING_VERIFICATION.value,
    )

    # Relationships
    staff: Mapped[StaffProfile] = relationship(back_populates="qualifications")

    __table_args__ = (
        Index("idx_staff_qualifications_staff_id", "staff_id"),
        Index(
            "idx_staff_qualifications_program",
            "program_type",
            postgresql_where=text("program_type IS NOT NULL"),
        ),
        # Used by the "expiring soon" background job / dashboard.
        Index(
            "idx_staff_qualifications_expiring",
            "expires_at",
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        CheckConstraint(
            "(expires_at IS NULL) OR (issued_at IS NULL) OR (expires_at >= issued_at)",
            name="ck_qualification_expires_after_issued",
        ),
    )


# --------------------------------------------------------------------------
# staff_availability
# --------------------------------------------------------------------------
class StaffAvailability(IdMixin, Base):
    """Either a recurring weekly window or a one-off block.

    Two flavours share this table — distinguished by which columns are set:

    - **Recurring weekly availability**: `day_of_week` (0=Mon..6=Sun) +
      `start_time` + `end_time`. `is_unavailable = false` means the staff
      member is free; `is_unavailable = true` means the recurring slot
      is a weekly block (e.g. "never schedule me on Friday afternoons").
    - **One-off window / block**: `specific_date` + `specific_start` +
      `specific_end`. Same `is_unavailable` semantic.

    The check constraint `ck_availability_recurring_or_specific` enforces
    that exactly one of the two flavours is populated.
    """

    __tablename__ = "staff_availability"

    staff_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("staff_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id", ondelete="CASCADE"),
        nullable=False,
    )
    is_unavailable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # ---- recurring fields (0=Mon..6=Sun per Python's isoweekday-1) ----
    day_of_week: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    start_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    end_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    # ---- one-off fields ----
    specific_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    specific_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    specific_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=text("now()"),
    )

    # Relationships
    staff: Mapped[StaffProfile] = relationship(back_populates="availability")

    __table_args__ = (
        Index("idx_staff_availability_staff_id", "staff_id"),
        Index(
            "idx_staff_availability_specific_date",
            "specific_date",
            postgresql_where=text("specific_date IS NOT NULL"),
        ),
        CheckConstraint(
            "(specific_date IS NOT NULL) <> (day_of_week IS NOT NULL)",
            name="ck_availability_recurring_or_specific",
        ),
        CheckConstraint(
            "(specific_end IS NULL OR specific_start IS NULL OR specific_end > specific_start) "
            "AND (end_time IS NULL OR start_time IS NULL OR end_time > start_time)",
            name="ck_availability_end_after_start",
        ),
    )


__all__ = ["StaffAvailability", "StaffProfile", "StaffQualification"]
