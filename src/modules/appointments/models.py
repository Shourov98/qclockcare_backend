"""Appointments module — ORM models for appointments + service items.

Tables:
- `appointments`                  — scheduled visit linking patient ↔ staff
- `appointment_service_items`     — line items under one appointment

Both tables are agency-scoped; RLS policies are defined in migration 0006.

Status lifecycle (enforced at service layer; see `service.py`):
  DRAFT → SCHEDULED → CONFIRMED → ASSIGNED → IN_PROGRESS → COMPLETED → PAID
                  ↘ CANCELLED   ↘ NO_SHOW   ↘ REJECTED
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.domain.base_entity import Base, IdMixin, TimestampedMixin
from src.shared.domain.enum_mapping import pg_name
from src.shared.domain.enums import (
    AppointmentEventType,
    AppointmentStatus,
    ConfirmationStatus,
    ProgramType,
    ServiceItemStatus,
    ServiceType,
    UserRole,
)

if TYPE_CHECKING:
    from src.modules.agencies.models import Agency
    from src.modules.patients.models import PatientProfile
    from src.modules.staff.models import StaffProfile
    from src.modules.visits.models import Visit


# --------------------------------------------------------------------------
# appointments
# --------------------------------------------------------------------------
class Appointment(IdMixin, TimestampedMixin, Base):
    """A scheduled visit by a staff member for a patient at one agency.

    Lifecycle statuses (see `AppointmentStatus`):
      - DRAFT              — created but not yet sent for confirmation
      - SCHEDULED          — sent, awaiting confirmation
      - NOTIFICATION_SENT  — patient/guardian notified
      - AWAITING_CONFIRMATION
      - CONFIRMED
      - RESCHEDULE_REQUESTED / CANCELLATION_REQUESTED
      - ASSIGNED           — staff confirmed/assigned to perform
      - CHECKED_IN
      - IN_PROGRESS
      - CHECKED_OUT
      - COMPLETED          — visit done; awaiting service verification
      - AWAITING_SERVICE_VERIFICATION
      - SERVICE_VERIFIED
      - DISPUTED
      - UNDER_REVIEW
      - APPROVED_FOR_BILLING
      - PAID
      - CANCELLED / NO_SHOW / REJECTED

    `staff_id` is nullable up to ASSIGNED (DRAFT / SCHEDULED may have no
    assignee yet). `confirmation_status` tracks the patient / guardian
    confirmation; `checked_in_at` / `checked_out_at` track the actual
    visit; `completed_at` records service completion.
    """

    __tablename__ = "appointments"

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
    staff_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("staff_profiles.id", ondelete="SET NULL"),
        nullable=True,
    )
    program_type: Mapped[ProgramType | None] = mapped_column(
        Enum(ProgramType, name=pg_name(ProgramType)),
        nullable=True,
    )

    # Window
    scheduled_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    scheduled_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # Status
    status: Mapped[AppointmentStatus] = mapped_column(
        Enum(AppointmentStatus, name=pg_name(AppointmentStatus)),
        nullable=False,
        default=AppointmentStatus.DRAFT,
        server_default=AppointmentStatus.DRAFT.value,
    )

    # Confirmation flow
    confirmation_status: Mapped[ConfirmationStatus | None] = mapped_column(
        Enum(ConfirmationStatus, name=pg_name(ConfirmationStatus)),
        nullable=True,
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    confirmation_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Visit timestamps
    checked_in_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    checked_out_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Context
    location: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Cancellation
    cancelled_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    agency: Mapped[Agency] = relationship(
        "Agency", back_populates="appointments"
    )
    patient: Mapped[PatientProfile] = relationship(
        "PatientProfile", back_populates="appointments"
    )
    staff: Mapped[StaffProfile | None] = relationship(
        "StaffProfile", back_populates="appointments"
    )
    service_items: Mapped[list[AppointmentServiceItem]] = relationship(
        back_populates="appointment",
        cascade="all, delete-orphan",
    )
    visit: Mapped[Visit | None] = relationship(
        "Visit",
        back_populates="appointment",
        cascade="all, delete-orphan",
        uselist=False,
    )

    __table_args__ = (
        Index("idx_appointments_agency_id", "agency_id"),
        Index("idx_appointments_patient_id", "patient_id"),
        Index(
            "idx_appointments_staff_id",
            "staff_id",
            postgresql_where=text("staff_id IS NOT NULL"),
        ),
        Index("idx_appointments_scheduled_start", "scheduled_start"),
        Index(
            "idx_appointments_status",
            "status",
            postgresql_where=text(
                "status IN ('SCHEDULED', 'CONFIRMED', 'ASSIGNED')"
            ),
        ),
        CheckConstraint(
            "scheduled_end > scheduled_start",
            name="ck_appointment_end_after_start",
        ),
        CheckConstraint(
            "(checked_in_at IS NULL) OR (checked_out_at IS NULL) OR "
            "(checked_out_at >= checked_in_at)",
            name="ck_appointment_checkout_after_checkin",
        ),
    )


# --------------------------------------------------------------------------
# appointment_service_items
# --------------------------------------------------------------------------
class AppointmentServiceItem(IdMixin, TimestampedMixin, Base):
    """A line item: one specific service to deliver during an appointment.

    `status` tracks per-item delivery (`PENDING → DONE / NOT_DONE /
    NOT_APPLICABLE / NEEDS_FOLLOW_UP`). When a visit is verified, the
    item statuses feed the billable line items. Service verification
    itself (the patient disputing an item) lives on a separate table
    in a later migration.
    """

    __tablename__ = "appointment_service_items"

    appointment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("appointments.id", ondelete="CASCADE"),
        nullable=False,
    )
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id", ondelete="CASCADE"),
        nullable=False,
    )
    service_type: Mapped[ServiceType] = mapped_column(
        Enum(ServiceType, name=pg_name(ServiceType)),
        nullable=False,
    )
    planned_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[ServiceItemStatus] = mapped_column(
        Enum(ServiceItemStatus, name=pg_name(ServiceItemStatus)),
        nullable=False,
        default=ServiceItemStatus.PENDING,
        server_default=ServiceItemStatus.PENDING.value,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    appointment: Mapped[Appointment] = relationship(back_populates="service_items")

    __table_args__ = (
        Index("idx_service_items_appointment_id", "appointment_id"),
        Index("idx_service_items_agency_id", "agency_id"),
        CheckConstraint(
            "(planned_minutes IS NULL) OR (planned_minutes > 0)",
            name="ck_service_item_planned_minutes_positive",
        ),
    )


__all__ = [
    "Appointment",
    "AppointmentConfirmation",
    "AppointmentEvent",
    "AppointmentServiceItem",
]


# --------------------------------------------------------------------------
# appointment_confirmations (1:1 with appointments)
# --------------------------------------------------------------------------
class AppointmentConfirmation(IdMixin, Base):
    """One row per confirmed appointment — captures WHO confirmed + HOW.

    Schema doc §10.3. The row is upserted on every confirmation attempt
    so a patient can re-confirm after a reschedule and the latest action
    wins. `confirmation_role` is captured at the time of the action
    (a guardian may confirm on behalf of the patient — the role is
    recorded so we can render an audit-quality timeline).
    """

    __tablename__ = "appointment_confirmations"

    appointment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("appointments.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    confirmed_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    confirmation_role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name=pg_name(UserRole)),
        nullable=False,
    )
    status: Mapped[ConfirmationStatus] = mapped_column(
        Enum(ConfirmationStatus, name=pg_name(ConfirmationStatus)),
        nullable=False,
    )
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    __table_args__ = (
        CheckConstraint(
            "confirmation_role IN ('PATIENT', 'GUARDIAN')",
            name="ck_appointment_confirmations_role",
        ),
        Index(
            "idx_appointment_confirmations_confirmed_by",
            "confirmed_by",
        ),
    )


# --------------------------------------------------------------------------
# appointment_events (append-only domain timeline)
# --------------------------------------------------------------------------
class AppointmentEvent(IdMixin, Base):
    """Immutable per-action event row for an appointment.

    Schema doc §10.4. Append-only at the application layer; the DB also
    enforces no UPDATE/DELETE via trigger (`trg_appointment_events_no_modify`).

    `event_type` is `text` (not a Postgres enum) so adding new event types
    doesn't require a migration. The app-layer enum `AppointmentEventType`
    is the source of truth for valid values.
    """

    __tablename__ = "appointment_events"

    appointment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("appointments.id", ondelete="CASCADE"),
        nullable=False,
    )
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id", ondelete="CASCADE"),
        nullable=False,
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_type: Mapped[AppointmentEventType] = mapped_column(
        Enum(AppointmentEventType, name=pg_name(AppointmentEventType), create_type=False),
        nullable=False,
    )
    from_status: Mapped[AppointmentStatus | None] = mapped_column(
        Enum(AppointmentStatus, name=pg_name(AppointmentStatus)),
        nullable=True,
    )
    to_status: Mapped[AppointmentStatus | None] = mapped_column(
        Enum(AppointmentStatus, name=pg_name(AppointmentStatus)),
        nullable=True,
    )
    metadata_: Mapped[dict] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    __table_args__ = (
        Index(
            "idx_appointment_events_appointment",
            "appointment_id",
            text("created_at"),
        ),
        Index(
            "idx_appointment_events_agency_date",
            "agency_id",
            text("created_at DESC"),
        ),
        CheckConstraint(
            "length(trim(event_type)) > 0",
            name="ck_appointment_events_type_non_empty",
        ),
    )
