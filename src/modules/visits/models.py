"""Visits module — ORM models for visits, service items, notes, verification, and issues.

Tables:
- `visits`                   — materialized attendance record (1:1 with an appointment)
- `visit_service_items`      — per-item delivery log under a visit
- `visit_notes`              — free-form narrative notes (clinical, operational)
- `service_verifications`    — patient/guardian post-visit verification (1:1 with visit)
- `visit_issues`             — non-blocking reports against a visit

All five are agency-scoped; RLS policies are defined in migration 0007.

Lifecycle:
- `Visit.status` walks CHECKED_IN → IN_PROGRESS → CHECKED_OUT → COMPLETED.
  The `Appointment.status` state machine (see appointments module) is
  driven independently — this module materializes the actual attendance
  record once the appointment is checked in.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.domain.base_entity import Base, IdMixin, TimestampedMixin
from src.shared.domain.enum_mapping import pg_name
from src.shared.domain.enums import (
    DisputeReasonCode,
    ServiceItemStatus,
    UserRole,
    VerificationStatus,
    VisitStatus,
)
from src.shared.utils.datetime_utils import utc_now

if TYPE_CHECKING:
    from src.modules.agencies.models import Agency
    from src.modules.appointments.models import Appointment, AppointmentServiceItem
    from src.modules.identity.models import User
    from src.modules.staff.models import StaffProfile


# --------------------------------------------------------------------------
# visits
# --------------------------------------------------------------------------
class Visit(IdMixin, TimestampedMixin, Base):
    """The materialized record of an appointment's actual attendance.

    Created when a staff member checks in for an appointment. One row
    per appointment at most (UNIQUE constraint). Holds all the visit-
    level context (GPS, device, duration) that the appointment row does
    not carry.
    """

    __tablename__ = "visits"

    appointment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("appointments.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id", ondelete="CASCADE"),
        nullable=False,
    )
    staff_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("staff_profiles.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[VisitStatus] = mapped_column(
        Enum(VisitStatus, name=pg_name(VisitStatus)),
        nullable=False,
        default=VisitStatus.CHECKED_IN,
        server_default=VisitStatus.CHECKED_IN.value,
    )

    # ---- check-in ----
    check_in_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    check_in_lat: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    check_in_lng: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    check_in_accuracy_m: Mapped[Decimal | None] = mapped_column(
        Numeric(6, 2), nullable=True
    )
    check_in_device_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    check_in_address_match: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    check_in_distance_from_location_m: Mapped[Decimal | None] = mapped_column(
        Numeric(8, 2), nullable=True
    )

    # ---- check-out ----
    check_out_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    check_out_lat: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    check_out_lng: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    check_out_accuracy_m: Mapped[Decimal | None] = mapped_column(
        Numeric(6, 2), nullable=True
    )
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Relationships
    appointment: Mapped[Appointment] = relationship(
        "Appointment", back_populates="visit"
    )
    agency: Mapped[Agency] = relationship("Agency")  # no back-ref needed
    staff: Mapped[StaffProfile] = relationship("StaffProfile")
    service_items: Mapped[list[VisitServiceItem]] = relationship(
        back_populates="visit",
        cascade="all, delete-orphan",
    )
    notes: Mapped[list[VisitNote]] = relationship(
        back_populates="visit",
        cascade="all, delete-orphan",
        order_by="VisitNote.created_at",
    )
    verification: Mapped[ServiceVerification | None] = relationship(
        back_populates="visit",
        cascade="all, delete-orphan",
        uselist=False,
    )
    issues: Mapped[list[VisitIssue]] = relationship(
        back_populates="visit",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index(
            "idx_visits_agency",
            "agency_id",
            text("check_in_time DESC"),
        ),
        Index(
            "idx_visits_staff",
            "staff_id",
            text("check_in_time DESC"),
        ),
        Index(
            "idx_visits_status",
            "status",
            postgresql_where=text("status <> 'COMPLETED'"),
        ),
        CheckConstraint(
            "(check_out_time IS NULL) OR (check_in_time IS NULL) OR "
            "(check_out_time > check_in_time)",
            name="ck_visit_checkout_after_checkin",
        ),
    )


# --------------------------------------------------------------------------
# visit_service_items
# --------------------------------------------------------------------------
class VisitServiceItem(IdMixin, TimestampedMixin, Base):
    """Per-item delivery log under a visit.

    Each row maps one appointment_service_item to the visit where it
    was actually delivered, with the staff-recorded outcome
    (DONE / NOT_DONE / etc.) plus optional reason and clinical note.
    """

    __tablename__ = "visit_service_items"

    visit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("visits.id", ondelete="CASCADE"),
        nullable=False,
    )
    appointment_service_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("appointment_service_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[ServiceItemStatus] = mapped_column(
        Enum(ServiceItemStatus, name=pg_name(ServiceItemStatus)),
        nullable=False,
        default=ServiceItemStatus.PENDING,
        server_default=ServiceItemStatus.PENDING.value,
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # Relationships
    visit: Mapped[Visit] = relationship(back_populates="service_items")
    appointment_service_item: Mapped[AppointmentServiceItem] = relationship(
        "AppointmentServiceItem"
    )
    completed_by_user: Mapped[User | None] = relationship("User")

    __table_args__ = (
        UniqueConstraint(
            "visit_id",
            "appointment_service_item_id",
            name="uq_visit_service_item",
        ),
        Index("idx_visit_service_items_visit", "visit_id"),
        CheckConstraint(
            "status <> 'NOT_DONE' OR (reason IS NOT NULL AND length(trim(reason)) > 0)",
            name="ck_reason_required_when_not_done",
        ),
    )


# --------------------------------------------------------------------------
# visit_notes
# --------------------------------------------------------------------------
class VisitNote(IdMixin, Base):
    """Free-form narrative note authored during/after the visit."""

    __tablename__ = "visit_notes"

    visit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("visits.id", ondelete="CASCADE"),
        nullable=False,
    )
    author_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=text("now()"),
    )

    # Relationships
    visit: Mapped[Visit] = relationship(back_populates="notes")
    author: Mapped[User] = relationship("User")

    __table_args__ = (
        Index("idx_visit_notes_visit", "visit_id", text("created_at")),
        CheckConstraint(
            "length(trim(body)) > 0",
            name="ck_visit_note_body_non_empty",
        ),
    )


# --------------------------------------------------------------------------
# service_verifications
# --------------------------------------------------------------------------
class ServiceVerification(IdMixin, Base):
    """Patient or guardian verification of a completed visit.

    1:1 with Visit (UNIQUE on visit_id). The verifier may be the patient
    themselves or a linked guardian. A status of DISPUTED must include
    a `dispute_reason_code` — enforced at DB and service layer.
    """

    __tablename__ = "service_verifications"

    visit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("visits.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id", ondelete="CASCADE"),
        nullable=False,
    )
    verified_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Either PATIENT or GUARDIAN per the schema doc.
    verifier_role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name=pg_name(UserRole)),
        nullable=False,
    )
    status: Mapped[VerificationStatus] = mapped_column(
        Enum(VerificationStatus, name=pg_name(VerificationStatus)),
        nullable=False,
    )
    dispute_reason_code: Mapped[DisputeReasonCode | None] = mapped_column(
        Enum(DisputeReasonCode, name=pg_name(DisputeReasonCode)),
        nullable=True,
    )
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=text("now()"),
    )

    # Relationships
    visit: Mapped[Visit] = relationship(back_populates="verification")
    verified_by_user: Mapped[User] = relationship("User")

    __table_args__ = (
        Index("idx_service_verifications_verified_by", "verified_by"),
        Index(
            "idx_service_verifications_agency",
            "agency_id",
            text("created_at DESC"),
        ),
        CheckConstraint(
            "verifier_role IN ('PATIENT', 'GUARDIAN')",
            name="ck_verifier_role_patient_or_guardian",
        ),
        CheckConstraint(
            "status <> 'DISPUTED' OR dispute_reason_code IS NOT NULL",
            name="ck_dispute_requires_reason",
        ),
    )


# --------------------------------------------------------------------------
# visit_issues
# --------------------------------------------------------------------------
class VisitIssue(IdMixin, Base):
    """Non-blocking report filed against a visit.

    Unlike ServiceVerification (which gates the billing pipeline),
    issues are informational. They can be resolved by an admin with a
    free-form resolution note; resolution is recorded but the row stays
    for history.
    """

    __tablename__ = "visit_issues"

    visit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("visits.id", ondelete="CASCADE"),
        nullable=False,
    )
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id", ondelete="CASCADE"),
        nullable=False,
    )
    reported_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    issue_type: Mapped[str] = mapped_column(Text, nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=text("now()"),
    )

    # Relationships
    visit: Mapped[Visit] = relationship(back_populates="issues")
    reported_by_user: Mapped[User] = relationship(
        "User", foreign_keys=[reported_by]
    )
    resolved_by_user: Mapped[User | None] = relationship(
        "User", foreign_keys=[resolved_by]
    )

    __table_args__ = (
        Index("idx_visit_issues_visit", "visit_id"),
        Index(
            "idx_visit_issues_unresolved",
            "agency_id",
            text("created_at DESC"),
            postgresql_where=text("resolved_at IS NULL"),
        ),
        CheckConstraint(
            "length(trim(issue_type)) > 0",
            name="ck_visit_issue_type_non_empty",
        ),
        CheckConstraint(
            "length(trim(comment)) > 0",
            name="ck_visit_issue_comment_non_empty",
        ),
    )


__all__ = [
    "ServiceVerification",
    "Visit",
    "VisitIssue",
    "VisitNote",
    "VisitServiceItem",
]
