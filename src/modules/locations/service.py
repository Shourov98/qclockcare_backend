"""Locations service — business logic for service-delivery addresses.

All queries are scoped to the caller's agency (defence in depth —
RLS is the actual security boundary; this layer makes the intent
explicit and produces cleaner log messages).

Reads filter out soft-deleted rows by default (`deleted_at IS NULL`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import NotFoundError, ValidationError
from src.modules.locations.models import Location
from src.modules.locations.schemas import (
    LocationCreateRequest,
    LocationUpdateRequest,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
async def _get_location_or_404(
    session: AsyncSession,
    *,
    location_id: uuid.UUID,
    agency_id: uuid.UUID,
    include_deleted: bool = False,
) -> Location:
    """Fetch one location scoped to the agency.

    Args:
        include_deleted: when True, soft-deleted rows are returned
            (used by the PATCH endpoint to allow restoring a row).

    Raises:
        NotFoundError: if not found OR if it belongs to another agency.
    """
    stmt = select(Location).where(
        Location.id == location_id,
        Location.agency_id == agency_id,
    )
    if not include_deleted:
        stmt = stmt.where(Location.deleted_at.is_(None))
    location = (await session.execute(stmt)).scalar_one_or_none()
    if location is None:
        raise NotFoundError(
            details={"resource": "location", "id": str(location_id)}
        )
    return location


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------
async def list_locations(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    page: int,
    page_size: int,
    include_inactive: bool = False,
) -> tuple[list[Location], int]:
    """List the agency's locations, offset-paginated.

    By default only `is_active=true` rows are returned. Set
    `include_inactive=true` to also surface deactivated locations.

    Returns (rows, total).
    """
    base = select(Location).where(
        Location.agency_id == agency_id,
        Location.deleted_at.is_(None),
    )
    if not include_inactive:
        base = base.where(Location.is_active.is_(True))

    total = (
        await session.execute(
            select(func.count()).select_from(base.subquery())
        )
    ).scalar_one()

    offset = (page - 1) * page_size
    rows = (
        await session.execute(
            base.order_by(Location.city, Location.label, Location.id)
            .offset(offset)
            .limit(page_size)
        )
    ).scalars().all()
    return list(rows), int(total)


async def get_location(
    session: AsyncSession,
    *,
    location_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> Location:
    """Fetch one active location (raises NotFoundError if missing)."""
    return await _get_location_or_404(
        session,
        location_id=location_id,
        agency_id=agency_id,
        include_deleted=False,
    )


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------
async def create_location(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    payload: LocationCreateRequest,
) -> Location:
    """Insert one new location row.

    Validates lat/lng pairing (the DB CHECK enforces it but we want a
    clean ValidationError before the round-trip).
    """
    if (payload.latitude is None) != (payload.longitude is None):
        raise ValidationError(
            "latitude and longitude must be provided together."
        )

    location = Location(
        agency_id=agency_id,
        label=payload.label,
        address_line1=payload.address_line1,
        address_line2=payload.address_line2,
        city=payload.city,
        state=payload.state,
        postal_code=payload.postal_code,
        country=payload.country,
        latitude=payload.latitude,
        longitude=payload.longitude,
        geofence_radius_m=payload.geofence_radius_m,
        is_active=payload.is_active,
    )
    session.add(location)
    await session.flush()
    return location


async def update_location(
    session: AsyncSession,
    *,
    location_id: uuid.UUID,
    agency_id: uuid.UUID,
    payload: LocationUpdateRequest,
) -> Location:
    """Apply a partial update to one location.

    Only fields explicitly set on `payload` are written (None vs
    "not provided" is distinguished by `model_fields_set`).
    """
    location = await _get_location_or_404(
        session,
        location_id=location_id,
        agency_id=agency_id,
        include_deleted=False,
    )

    updates = payload.model_dump(exclude_unset=True)

    # Cross-field validation: lat/lng must move together.
    new_lat = updates.get("latitude", location.latitude)
    new_lng = updates.get("longitude", location.longitude)
    if (new_lat is None) != (new_lng is None):
        raise ValidationError(
            "latitude and longitude must be provided together."
        )

    for field, value in updates.items():
        setattr(location, field, value)
    await session.flush()
    return location


async def soft_delete_location(
    session: AsyncSession,
    *,
    location_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> Location:
    """Mark the location as deleted (preserves history for FK references).

    Idempotent: deleting an already-deleted row returns the same row
    without re-stamping `deleted_at`.
    """
    location = await _get_location_or_404(
        session,
        location_id=location_id,
        agency_id=agency_id,
        include_deleted=True,
    )
    if location.deleted_at is None:
        location.deleted_at = datetime.now(UTC)
    await session.flush()
    return location


__all__ = [
    "create_location",
    "get_location",
    "list_locations",
    "soft_delete_location",
    "update_location",
]
