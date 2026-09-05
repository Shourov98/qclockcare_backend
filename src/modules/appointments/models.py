"""Appointments module — ORM models for appointments + activities.

Tables:
- `appointments`            — scheduled visit linking patient ↔ staff
- `appointment_activities`  — checklist of free-text activities the
                              caregiver must complete during the visit

Both tables are agency-scoped; RLS policies live in
`alembic/versions/0027_appointment_flow_alignment.py`.

Lifecycle (see `AppointmentStatus`):
  SCHEDULED → READY → IN_PROGRESS → AWAITING_SIGNATURE → COMPLETED
              ↘  CANCELLED, MISSED, REJECTED  ↙

Visit-side artifacts (`evv_records`, `appointment_signatures`,
`visit.billing_confirmed_at`) live under `src/modules/visits/models.py`.
This module owns only the appointment and its activities.
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
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.domain.base_entity import Base, IdMixin, TimestampedMixin
from src.shared.domain.enum_mapping import pg_name
from src.shared.domain.enums import (
    AppointmentStatus,
    ProgramType,
    ServiceItemStatus,
)

if TYPE_CHECKING:
    from src.modules.agencies.models import Agency
    from src.modules.locations.models import Location
    from src.modules.patients.models import PatientProfile
    from src.modules.staff.models import StaffProfile
    from src.modules.visits.models import Visit


# --------------------------------------------------------------------------
# appointments
# --------------------------------------------------------------------------
class Appointment(IdMixin, TimestampedMixin, Base):
    """A scheduled visit by a staff member for a patient at one agency.

    Lifecycle (see `AppointmentStatus`):
      - SCHEDULED          — created + staff assigned, not yet active
      - READY              — admin marked it ready (caregiver notified)
      - IN_PROGRESS        — caregiver started the visit (POST /visits)
      - AWAITING_SIGNATURE — caregiver submitted End Task; awaiting
                             patient/guardian signature
      - COMPLETED          — signature received
      - CANCELLED / MISSED / REJECTED — exception edges

    `staff_id` is required from `READY` onward (DB-enforced via RLS +
    service-layer check). The signature flow replaces the legacy
    confirmation flow; the patient/guardian signs at the visit end, not
    at scheduling time. EVV start/end records live on
    `evv_records` (1:1 with the materialized visit).
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

    # Window — duration is derived (scheduled_end - scheduled_start) per
    # spec §1. The DB enforces the ordering.
    scheduled_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    scheduled_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # Status (5-state lifecycle per spec).
    status: Mapped[AppointmentStatus] = mapped_column(
        Enum(AppointmentStatus, name=pg_name(AppointmentStatus)),
        nullable=False,
        default=AppointmentStatus.SCHEDULED,
        server_default=AppointmentStatus.SCHEDULED.value,
    )

    # Context
    location: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Structured location — points to a row in the `locations` table
    # which carries lat/lng + structured address. Nullable so legacy
    # free-text `location` rows keep working; new appointments should
    # prefer this FK so the FE can render a map pin.
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("locations.id", ondelete="SET NULL"),
        nullable=True,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Cancellation (exception edge)
    cancelled_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Billing — denormalized onto the appointment row so the agency-admin
    # dashboard / visit-summary screen can render "Paid" / "Unpaid"
    # without joining to `visits`. The visit row keeps its own
    # `billing_confirmed_at` (timestamp of the staff confirmation); this
    # promotion lets the FE render the badge straight off the appointment
    # payload. `claim_id` is the durable, externally-rendered identifier
    # generated at insert time as `CG-{agency_code_short}-{appt_short}`.
    billing_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unpaid", server_default="unpaid"
    )
    billing_paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    billing_paid_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    claim_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, unique=True
    )

    # Relationships
    agency: Mapped[Agency] = relationship(
        "Agency", back_populates="appointments"
    )
    location_rel: Mapped["Location | None"] = relationship(
        "Location",
        foreign_keys=[location_id],
        lazy="raise",  # never implicit — eager-load explicitly via selectinload
    )
    patient: Mapped[PatientProfile] = relationship(
        "PatientProfile", back_populates="appointments"
    )
    staff: Mapped[StaffProfile | None] = relationship(
        "StaffProfile", back_populates="appointments"
    )
    activities: Mapped[list[AppointmentActivity]] = relationship(
        back_populates="appointment",
        cascade="all, delete-orphan",
        order_by="AppointmentActivity.created_at",
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
            "idx_appointments_status_active",
            "status",
            postgresql_where=text(
                "status IN ('SCHEDULED', 'READY', 'IN_PROGRESS', "
                "'AWAITING_SIGNATURE')"
            ),
        ),
        Index(
            "idx_appointments_agency_location",
            "agency_id",
            "location_id",
            postgresql_where=text("location_id IS NOT NULL"),
        ),
        CheckConstraint(
            "scheduled_end > scheduled_start",
            name="ck_appointment_end_after_start",
        ),
    )


# --------------------------------------------------------------------------
# appointment_activities
# --------------------------------------------------------------------------
class AppointmentActivity(IdMixin, TimestampedMixin, Base):
    """A free-text activity the caregiver must complete during the visit.

    Per the spec (`QlockCare_appointemnt_flow.md` §2), activities are
    free-text names entered by the Agency Admin at scheduling time:
      - "Check blood pressure"
      - "Assist with medication"
      - "Prepare meal"
      - "Help patient walk"
      - "Perform hygiene assistance"
      - "Record patient condition"

    The caregiver cannot submit "End Task" until every activity is in
    `DONE` (or `NOT_APPLICABLE`) — enforced by the service layer on
    the IN_PROGRESS → AWAITING_SIGNATURE transition.

    Replaces the legacy `appointment_service_items` table (which used
    an enum `service_type`); the rename is handled by migration 0027
    plus a backfill that renders the old enum into the new `name`
    column via `humanize_enum`.
    """

    __tablename__ = "appointment_activities"

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
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    planned_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[ServiceItemStatus] = mapped_column(
        Enum(ServiceItemStatus, name=pg_name(ServiceItemStatus)),
        nullable=False,
        default=ServiceItemStatus.PENDING,
        server_default=ServiceItemStatus.PENDING.value,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # When the caregiver marked it DONE / NOT_DONE.
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Relationships
    appointment: Mapped[Appointment] = relationship(back_populates="activities")

    __table_args__ = (
        Index("idx_activities_appointment_id", "appointment_id"),
        Index("idx_activities_agency_id", "agency_id"),
        Index(
            "idx_activities_pending",
            "appointment_id",
            postgresql_where=text("status = 'PENDING'"),
        ),
        CheckConstraint(
            "(planned_minutes IS NULL) OR (planned_minutes > 0)",
            name="ck_activity_planned_minutes_positive",
        ),
        CheckConstraint(
            "length(trim(name)) > 0",
            name="ck_activity_name_non_empty",
        ),
    )


__all__ = [
    "Appointment",
    "AppointmentActivity",
]
