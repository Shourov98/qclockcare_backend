from __future__ import annotations

import uuid
from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from src.shared.domain.base_entity import Base, IdMixin, TimestampedMixin


class GroupHome(IdMixin, TimestampedMixin, Base):
    __tablename__ = "group_homes"
    agency_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agencies.id", ondelete="CASCADE"), nullable=False)
    location_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False, unique=True)
    latitude: Mapped[float] = mapped_column(Numeric(9, 6), nullable=False)
    longitude: Mapped[float] = mapped_column(Numeric(9, 6), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, server_default="4")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    guardian_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("guardian_profiles.id", ondelete="SET NULL"), nullable=True)
    owner_patient_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("patient_profiles.id", ondelete="SET NULL"), nullable=True)

    __table_args__ = (
        CheckConstraint("latitude BETWEEN -90 AND 90", name="group_homes_latitude_range"),
        CheckConstraint("longitude BETWEEN -180 AND 180", name="group_homes_longitude_range"),
        CheckConstraint(
            "(guardian_id IS NOT NULL AND owner_patient_id IS NULL) OR "
            "(guardian_id IS NULL AND owner_patient_id IS NOT NULL)",
            name="group_homes_one_owner",
        ),
    )


class GroupHomeMember(IdMixin, TimestampedMixin, Base):
    __tablename__ = "group_home_members"
    group_home_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("group_homes.id", ondelete="CASCADE"), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("patient_profiles.id", ondelete="CASCADE"), nullable=False)
    removed_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("group_home_id", "patient_id", name="uq_group_home_member"),)


class GroupHomeAppointment(IdMixin, TimestampedMixin, Base):
    __tablename__ = "group_home_appointments"
    agency_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agencies.id", ondelete="CASCADE"), nullable=False)
    group_home_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("group_homes.id", ondelete="CASCADE"), nullable=False)
    staff_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("staff_profiles.id", ondelete="SET NULL"), nullable=True)
    service: Mapped[str] = mapped_column(Text, nullable=False)
    scheduled_start: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False)
    scheduled_end: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="SCHEDULED")


class GroupHomeAppointmentPatient(IdMixin, Base):
    __tablename__ = "group_home_appointment_patients"
    group_appointment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("group_home_appointments.id", ondelete="CASCADE"), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("patient_profiles.id", ondelete="RESTRICT"), nullable=False)
    __table_args__ = (UniqueConstraint("group_appointment_id", "patient_id", name="uq_group_appointment_patient"),)
