"""Patients module — ORM models for patient + guardian + relationship tables.

Tables:
- `patient_profiles`                — per-agency care-recipient record
- `guardian_profiles`               — per-agency authorised person
- `patient_guardian_relationships`  — many-to-many patient ↔ guardian

All three are agency-scoped; RLS policies are defined in migration 0005.

Lifecycle notes:
- `status` on both profile tables reuses `user_status` (INVITED → ACTIVE →
  INACTIVE/ARCHIVED). This keeps the lifecycle of a User, Patient, Guardian,
  and Staff row consistent across the platform.
- Soft-delete (`deleted_at`) is used on the profiles. Relationship rows are
  never soft-deleted — instead, the `valid_until` column marks them
  inactive. We keep history.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.domain.base_entity import Base, IdMixin, TimestampedMixin
from src.shared.domain.enum_mapping import pg_name
from src.shared.domain.enums import RelationshipType, UserStatus
from src.shared.utils.datetime_utils import utc_now

if TYPE_CHECKING:
    from src.modules.agencies.models import Agency
    from src.modules.identity.models import User
    from src.modules.appointments.models import Appointment


# --------------------------------------------------------------------------
# patient_profiles
# --------------------------------------------------------------------------
class PatientProfile(IdMixin, TimestampedMixin, Base):
    """A care-recipient's profile at a single agency.

    One `User` can hold patient profiles at multiple agencies (e.g. a
    person who moves between care providers); the `(agency_id, user_id)`
    unique constraint prevents duplicate profiles at one agency.
    `patient_code` is the per-agency human-readable identifier.

    Soft-delete is supported via `deleted_at`; an explicit
    `discharged_at` date marks clinical end of service, which can differ
    from the soft-delete (e.g. "discharged but kept on file for records").
    """

    __tablename__ = "patient_profiles"

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
    patient_code: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, name=pg_name(UserStatus)),
        nullable=False,
        default=UserStatus.INVITED,
        server_default=UserStatus.INVITED.value,
    )

    # Demographic / clinical metadata (all optional)
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    gender: Mapped[str | None] = mapped_column(Text, nullable=True)
    preferred_language: Mapped[str | None] = mapped_column(Text, nullable=True)
    care_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Enrolment dates
    admitted_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    discharged_at: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Relationships
    agency: Mapped["Agency"] = relationship(  # noqa: F821
        "Agency", back_populates="patient_profiles"
    )
    user: Mapped["User"] = relationship(  # noqa: F821
        "User", back_populates="patient_profiles"
    )
    guardian_links: Mapped[list["PatientGuardianRelationship"]] = relationship(
        back_populates="patient",
        cascade="all, delete-orphan",
    )
    appointments: Mapped[list["Appointment"]] = relationship(  # noqa: F821
        "Appointment", back_populates="patient", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint(
            "agency_id", "user_id", name="uq_patient_per_user_per_agency"
        ),
        UniqueConstraint(
            "agency_id", "patient_code", name="uq_patient_code_per_agency"
        ),
        Index(
            "idx_patient_profiles_agency_id",
            "agency_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("idx_patient_profiles_user_id", "user_id"),
        CheckConstraint(
            "(discharged_at IS NULL) OR (admitted_at IS NULL) OR (discharged_at >= admitted_at)",
            name="ck_patient_discharged_after_admitted",
        ),
    )


# --------------------------------------------------------------------------
# guardian_profiles
# --------------------------------------------------------------------------
class GuardianProfile(IdMixin, TimestampedMixin, Base):
    """A person authorised to act on behalf of one or more patients.

    Same shape as `PatientProfile`: a User at an Agency, with `(agency_id,
    user_id)` unique. We don't track a "guardian_code" because guardians
    are usually contacted rather than badge-looked-up — but `contact_phone`
    and `contact_email` may differ from the user's account (e.g. a
    conservator's office line).
    """

    __tablename__ = "guardian_profiles"

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
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, name=pg_name(UserStatus)),
        nullable=False,
        default=UserStatus.INVITED,
        server_default=UserStatus.INVITED.value,
    )
    contact_phone: Mapped[str | None] = mapped_column(Text, nullable=True)
    contact_email: Mapped[str | None] = mapped_column(CITEXT(), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    agency: Mapped["Agency"] = relationship(  # noqa: F821
        "Agency", back_populates="guardian_profiles"
    )
    user: Mapped["User"] = relationship(  # noqa: F821
        "User", back_populates="guardian_profiles"
    )
    patient_links: Mapped[list["PatientGuardianRelationship"]] = relationship(
        back_populates="guardian",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint(
            "agency_id", "user_id", name="uq_guardian_per_user_per_agency"
        ),
        Index(
            "idx_guardian_profiles_agency_id",
            "agency_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("idx_guardian_profiles_user_id", "user_id"),
    )


# --------------------------------------------------------------------------
# patient_guardian_relationships
# --------------------------------------------------------------------------
class PatientGuardianRelationship(IdMixin, TimestampedMixin, Base):
    """Links a patient to a guardian at a single agency.

    Composite uniqueness on `(agency_id, patient_id, guardian_id,
    relationship_type)` — the same guardian may hold multiple
    relationship types to the same patient (e.g. PARENT + GUARDIAN), but
    never duplicate (patient, guardian, type) tuples.

    `is_legal = true` marks a relationship that grants legal authority
    (sign-off, consent, etc.). Only `is_legal` relationships may act on
    a service verification or similar — enforced at the service layer.

    `valid_from` / `valid_until` form an open-ended validity window. Set
    `valid_until` to revoke a relationship without losing history.
    """

    __tablename__ = "patient_guardian_relationships"

    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id", ondelete="CASCADE"),
        nullable=False,
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("patient_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    guardian_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("guardian_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    relationship_type: Mapped[RelationshipType] = mapped_column(
        Enum(RelationshipType, name=pg_name(RelationshipType)),
        nullable=False,
    )
    is_legal: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Relationships
    patient: Mapped[PatientProfile] = relationship(back_populates="guardian_links")
    guardian: Mapped[GuardianProfile] = relationship(back_populates="patient_links")

    __table_args__ = (
        UniqueConstraint(
            "agency_id",
            "patient_id",
            "guardian_id",
            "relationship_type",
            name="uq_patient_guardian_rel",
        ),
        Index("idx_pgr_patient_id", "patient_id"),
        Index("idx_pgr_guardian_id", "guardian_id"),
        Index("idx_pgr_agency_id", "agency_id"),
        CheckConstraint(
            "(valid_until IS NULL) OR (valid_from IS NULL) OR (valid_until >= valid_from)",
            name="ck_relationship_valid_dates",
        ),
    )


__all__ = [
    "GuardianProfile",
    "PatientGuardianRelationship",
    "PatientProfile",
]