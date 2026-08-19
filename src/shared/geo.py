"""Geo helpers — haversine distance + geofence checks.

Pure Python; no PostGIS / earthdistance extension required. Used by the
visits service to **server-compute** `check_in_distance_from_location_m`
and `check_in_address_match` (previously client-supplied and therefore
untrusted). Coordinates may be `Decimal`, `float`, `int`, or `None`; all
arithmetic goes through `float()` so we don't depend on the input type.

Distance formula: standard great-circle (haversine). Accuracy is ±0.5%
globally — well within the geofence radius tolerance.

`within_geofence(...)` returns `(within, distance_m)`. `within` is
`False` whenever the distance can't be computed (any coordinate is
missing) — callers must interpret `None` distance as "unknown / not
within geofence" rather than "within".
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Final

# Mean Earth radius in metres (WGS-84 sphere approximation).
EARTH_RADIUS_M: Final[float] = 6_371_000.0

# Default geofence radius when no value is configured on the appointment
# or its linked location row.
DEFAULT_GEOFENCE_RADIUS_M: Final[int] = 150


def _as_float(value: Decimal | float | int | str | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def haversine_m(
    lat1: Decimal | float | int | str | None,
    lng1: Decimal | float | int | str | None,
    lat2: Decimal | float | int | str | None,
    lng2: Decimal | float | int | str | None,
) -> float | None:
    """Great-circle distance in metres between two lat/lng pairs.

    Returns `None` when any input is `None` or otherwise non-numeric.
    """
    f_lat1 = _as_float(lat1)
    f_lng1 = _as_float(lng1)
    f_lat2 = _as_float(lat2)
    f_lng2 = _as_float(lng2)
    if f_lat1 is None or f_lng1 is None or f_lat2 is None or f_lng2 is None:
        return None

    phi1 = math.radians(f_lat1)
    phi2 = math.radians(f_lat2)
    d_phi = math.radians(f_lat2 - f_lat1)
    d_lambda = math.radians(f_lng2 - f_lng1)

    a = (
        math.sin(d_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2
    )
    c = 2.0 * math.asin(min(1.0, math.sqrt(a)))
    return EARTH_RADIUS_M * c


def within_geofence(
    staff_lat: Decimal | float | int | str | None,
    staff_lng: Decimal | float | int | str | None,
    target_lat: Decimal | float | int | str | None,
    target_lng: Decimal | float | int | str | None,
    radius_m: float | int | None,
) -> tuple[bool, float | None]:
    """Check whether the staff position is inside a circular geofence.

    Returns `(within, distance_m)`. `distance_m` is `None` if the
    computation was impossible; `within` is `False` in that case (treat
    missing data as "not within").
    """
    distance = haversine_m(staff_lat, staff_lng, target_lat, target_lng)
    if distance is None:
        return False, None
    radius = DEFAULT_GEOFENCE_RADIUS_M if radius_m is None else float(radius_m)
    return (distance <= radius, distance)


__all__ = [
    "DEFAULT_GEOFENCE_RADIUS_M",
    "EARTH_RADIUS_M",
    "haversine_m",
    "within_geofence",
]