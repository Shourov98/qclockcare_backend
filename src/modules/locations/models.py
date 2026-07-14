"""Locations module — ORM model for `locations`.

One row = one service-delivery address at one agency. Used by
appointments and visits to indicate where the service happens.
Soft-deletable (`deleted_at`) so historical appointments still
reference a valid location.

Fields:
  - Postal address (line1/line2/city/state/postal/country).
  - Optional lat/lng + geofence radius (used by visit check-in).
  - `is_active` — admin toggle for new-appointment eligibility.
  - `label` — short human-readable name ("Home", "Day Program").

Constraints (enforced both here and in the migration):
  - state is exactly 2 characters (US state).
  - postal_code is non-empty after trim.
  - lat/lng are either both NULL or both in valid WGS-84 range.
  - geofence_radius_m is between 10 and 5000 meters.

RLS lives in the migration (0011_locations).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.domain.base_entity import Base, IdMixin, SoftDeleteMixin, TimestampedMixin


class Location(IdMixin, TimestampedMixin, SoftDeleteMixin, Base):
    """A service-delivery address at one agency.

    `deleted_at` filters the row out of default reads (the service
    layer always adds `WHERE deleted_at IS NULL`). `is_active` is a
    separate flag — an inactive location can still be referenced by
    past appointments/visits but cannot be chosen for new ones.
    """

    __tablename__ = "locations"

    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id", ondelete="CASCADE"),
        nullable=False,
    )
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    address_line1: Mapped[str] = mapped_column(Text, nullable=False)
    address_line2: Mapped[str | None] = mapped_column(Text, nullable=True)
    city: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    postal_code: Mapped[str] = mapped_column(Text, nullable=False)
    country: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'US'")
    )
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    geofence_radius_m: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("150")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )

    __table_args__ = (
        CheckConstraint(
            "length(trim(state)) = 2",
            name="ck_locations_state_two_letters",
        ),
        CheckConstraint(
            "length(trim(postal_code)) > 0",
            name="ck_locations_postal_code_non_empty",
        ),
        CheckConstraint(
            "(latitude IS NULL AND longitude IS NULL) OR "
            "(latitude IS NOT NULL AND longitude IS NOT NULL "
            "AND latitude BETWEEN -90 AND 90 "
            "AND longitude BETWEEN -180 AND 180)",
            name="ck_locations_lat_lng_pair",
        ),
        CheckConstraint(
            "geofence_radius_m BETWEEN 10 AND 5000",
            name="ck_locations_geofence_radius_range",
        ),
        Index(
            "idx_locations_agency_id",
            "agency_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "idx_locations_agency_label",
            "agency_id",
            "label",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


__all__ = ["Location"]
