"""Visits module — ORM models for the materialized attendance record.

Tables (after migration 0027):
- `visits`                 — 1:1 with an appointment; the visit exists
                              once the staff app POSTs `/visits`
- `visit_activity_deliveries` — per-visit copy of the parent
                              appointment's activities (staff records
                              what was actually delivered)
- `visit_notes`            — free-form narrative notes
- `evv_records`            — Electronic Visit Verification start + end
                              (split out per spec §10)
- `appointment_signatures` — required patient-or-guardian signature
                              (1:1 with visit; replaces service_verifications)

Lifecycle (`VisitStatus` mirrors `AppointmentStatus`):
  SCHEDULED → READY → IN_PROGRESS → AWAITING_SIGNATURE → COMPLETED
              ↘ CANCELLED / MISSED / REJECTED ↙

The visit row is created when the staff app POSTs `/visits` (transition
READY → IN_PROGRESS). The visit walks the same 5-state path as the
appointment until COMPLETED.

RLS policies are applied in migration 0027
(`alembic/versions/0027_appointment_flow_alignment.py`).
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
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import INET, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.domain.base_entity import Base, IdMixin, TimestampedMixin
from src.shared.domain.enum_mapping import pg_name
from src.shared.domain.enums import (
    AppointmentStatus,
    ServiceItemStatus,
    UserRole,
    VisitStatus,
)
from src.shared.utils.datetime_utils import utc_now

if TYPE_CHECKING:
    from src.modules.agencies.models import Agency
    from src.modules.appointments.models import Appointment, AppointmentActivity
    from src.modules.identity.models import User
    from src.modules.staff.models import StaffProfile


# --------------------------------------------------------------------------
# visits
# --------------------------------------------------------------------------
class Visit(IdMixin, TimestampedMixin, Base):
    """The materialized record of an appointment's actual attendance.

    Created when the staff app POSTs `/visits` (transition
    `READY → IN_PROGRESS`). One row per appointment at most
    (UNIQUE constraint). Holds the live-GPS stream and the
    `billing_confirmed_at` gate the caregiver sets before submitting
    "End Task".

    Per spec, the GPS / device / EVV start+end columns moved to the
    sibling `evv_records` table; duration is derived from
    `evv_records.start_time` + `end_time`.
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
        default=VisitStatus.IN_PROGRESS,
        server_default=VisitStatus.IN_PROGRESS.value,
    )

    # Spec §6: caregiver ticks "I confirm the visit and billing
    # information is correct" before submitting End Task. We model that
    # as a single timestamp set by `POST /visits/{id}/confirm-billing`.
    # Required before `IN_PROGRESS → AWAITING_SIGNATURE`.
    billing_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    billing_confirmed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ---- live location (staff opt-in while IN_PROGRESS) ----
    # Updated by `POST /visits/{id}/location-ping`. Used by the EVV
    # Live Monitor to render a moving marker per visit.
    # `sharing_location` is the user's opt-in flag — when False the
    # pings are ignored even if the device sends them.
    live_lat: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    live_lng: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    live_ping_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    live_accuracy_m: Mapped[Decimal | None] = mapped_column(
        Numeric(6, 2), nullable=True
    )
    sharing_location: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    # Relationships
    appointment: Mapped[Appointment] = relationship(
        "Appointment", back_populates="visit"
    )
    agency: Mapped[Agency] = relationship("Agency")  # no back-ref needed
    staff: Mapped[StaffProfile] = relationship(
        "StaffProfile",
        # Two FKs exist between `visits` and `staff_profiles`:
        #  - `visits.staff_id`           -> `staff_profiles.id`  (assigned staff)
        #  - `staff_profiles.last_known_visit_id` -> `visits.id`  (last visit)
        # Disambiguate `Visit.staff` to the first FK so the mapper doesn't
        # raise AmbiguousForeignKeysError during configuration.
        foreign_keys=[staff_id],
    )
    evv_record: Mapped[EVVRecord | None] = relationship(
        back_populates="visit",
        cascade="all, delete-orphan",
        uselist=False,
    )
    signature: Mapped[AppointmentSignature | None] = relationship(
        back_populates="visit",
        cascade="all, delete-orphan",
        uselist=False,
    )
    activity_deliveries: Mapped[list[VisitActivityDelivery]] = relationship(
        back_populates="visit",
        cascade="all, delete-orphan",
    )
    notes: Mapped[list[VisitNote]] = relationship(
        back_populates="visit",
        cascade="all, delete-orphan",
        order_by="VisitNote.created_at",
    )

    __table_args__ = (
        Index("idx_visits_agency", "agency_id"),
        Index("idx_visits_staff", "staff_id"),
        Index(
            "idx_visits_status_active",
            "status",
            postgresql_where=text(
                "status IN ('IN_PROGRESS', 'AWAITING_SIGNATURE')"
            ),
        ),
        Index(
            "idx_visits_live_ping",
            "agency_id",
            text("live_ping_at DESC"),
            postgresql_where=text("sharing_location = true"),
        ),
    )


# --------------------------------------------------------------------------
# evv_records
# --------------------------------------------------------------------------
class EVVRecord(IdMixin, TimestampedMixin, Base):
    """Electronic Visit Verification start + end (1:1 with a Visit).

    Per spec §10, the EVV lifecycle is two records:
      - EVV Start (caregiver arrival): time + GPS + device
      - EVV End   (caregiver departure): time + GPS
    We keep both on one row so the EVV page renders in a single query.

    `start_verification_status` is derived (`PENDING | VERIFIED |
    FAILED`) — it's set by the staff app on POST and recomputed when
    the live GPS pings come in.
    """

    __tablename__ = "evv_records"

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

    # ---- EVV Start ----
    start_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    start_lat: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    start_lng: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    start_accuracy_m: Mapped[Decimal | None] = mapped_column(
        Numeric(6, 2), nullable=True
    )
    start_device_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    start_verification_status: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )

    # ---- EVV End ----
    end_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    end_lat: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    end_lng: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    end_accuracy_m: Mapped[Decimal | None] = mapped_column(
        Numeric(6, 2), nullable=True
    )

    # Relationships
    visit: Mapped[Visit] = relationship(back_populates="evv_record")

    __table_args__ = (
        Index("idx_evv_records_agency", "agency_id"),
        CheckConstraint(
            "(end_time IS NULL) OR (start_time IS NULL) OR "
            "(end_time >= start_time)",
            name="ck_evv_end_after_start",
        ),
        CheckConstraint(
            "start_verification_status IS NULL OR "
            "start_verification_status IN ('PENDING', 'VERIFIED', 'FAILED')",
            name="ck_evv_verification_status_enum",
        ),
    )


# --------------------------------------------------------------------------
# appointment_signatures
# --------------------------------------------------------------------------
class AppointmentSignature(IdMixin, Base):
    """Required patient-or-guardian signature on a completed visit.

    Per spec §8-9, the signature is MANDATORY before the visit can
    transition `AWAITING_SIGNATURE → COMPLETED`. The signer may be the
    patient or a linked guardian; either satisfies the gate.

    `signer_display_name` is rendered as `"J. Smith"` (first letter +
    last name) per spec §9 — computed at write-time by
    `signer_display_name()` in `src.shared.utils.labels`.

    `signature_image_url` is a relative path under
    `SIGNATURE_STORAGE_PATH` (local FS for now; S3 wiring deferred).
    """

    __tablename__ = "appointment_signatures"

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
    signer_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    signer_role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name=pg_name(UserRole)),
        nullable=False,
    )
    signer_display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    signature_image_url: Mapped[str] = mapped_column(Text, nullable=False)
    signed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=text("now()"),
    )
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    visit: Mapped[Visit] = relationship(back_populates="signature")
    signer: Mapped[User] = relationship("User", foreign_keys=[signer_user_id])

    __table_args__ = (
        Index(
            "idx_appointment_signatures_agency",
            "agency_id",
            text("signed_at DESC"),
        ),
        Index("idx_appointment_signatures_signer", "signer_user_id"),
        CheckConstraint(
            "signer_role IN ('PATIENT', 'GUARDIAN')",
            name="ck_signature_signer_role",
        ),
        CheckConstraint(
            "length(trim(signer_display_name)) > 0",
            name="ck_signature_display_name_non_empty",
        ),
        CheckConstraint(
            "length(trim(signature_image_url)) > 0",
            name="ck_signature_image_url_non_empty",
        ),
    )


# --------------------------------------------------------------------------
# visit_activity_deliveries
# --------------------------------------------------------------------------
class VisitActivityDelivery(IdMixin, TimestampedMixin, Base):
    """Per-visit copy of the parent appointment's activities.

    Each row maps one `appointment_activities.id` to the visit where
    it was actually delivered, with the staff-recorded outcome
    (`DONE / NOT_DONE / NOT_APPLICABLE / NEEDS_FOLLOW_UP`) plus an
    optional reason (`NOT_DONE` requires reason — DB-enforced) and
    optional clinical note.

    Spec §5: every activity must be `DONE` (or `NOT_APPLICABLE`) before
    the caregiver can submit End Task — enforced by the service layer
    on the `IN_PROGRESS → AWAITING_SIGNATURE` transition.
    """

    __tablename__ = "visit_activity_deliveries"

    visit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("visits.id", ondelete="CASCADE"),
        nullable=False,
    )
    activity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("appointment_activities.id", ondelete="CASCADE"),
        nullable=False,
    )
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id", ondelete="CASCADE"),
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
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Relationships
    visit: Mapped[Visit] = relationship(back_populates="activity_deliveries")
    activity: Mapped[AppointmentActivity] = relationship("AppointmentActivity")
    completed_by_user: Mapped[User | None] = relationship(
        "User", foreign_keys=[completed_by]
    )

    __table_args__ = (
        UniqueConstraint(
            "visit_id",
            "activity_id",
            name="uq_visit_activity",
        ),
        Index("idx_visit_activity_deliveries_visit", "visit_id"),
        CheckConstraint(
            "status <> 'NOT_DONE' OR "
            "(reason IS NOT NULL AND length(trim(reason)) > 0)",
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
    author: Mapped[User] = relationship("User", foreign_keys=[author_user_id])

    __table_args__ = (
        Index("idx_visit_notes_visit", "visit_id", text("created_at")),
        CheckConstraint(
            "length(trim(body)) > 0",
            name="ck_visit_note_body_non_empty",
        ),
    )


# Re-export for type-checkers that previously imported the legacy
# `AppointmentStatus` alias from this module via the old `verification`
# relationship. Kept as a no-op so legacy imports don't break during
# the transition.
_ = AppointmentStatus  # noqa: F841 — referenced for future use


__all__ = [
    "AppointmentSignature",
    "EVVRecord",
    "Visit",
    "VisitActivityDelivery",
    "VisitNote",
]
