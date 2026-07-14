"""Unit tests for locations schemas — validation + ORM shape.

Covers:
  - LocationCreateRequest: required fields, state normalization,
    lat/lng pairing check is at the service layer.
  - LocationUpdateRequest: all fields optional, state normalization.
  - LocationResponse: ORM round-trip.
  - LocationListResponse: pagination envelope.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.modules.locations.models import Location
from src.modules.locations.schemas import (
    LocationCreateRequest,
    LocationListResponse,
    LocationResponse,
    LocationUpdateRequest,
)


class TestLocationCreateRequest:
    def test_minimal_valid(self) -> None:
        r = LocationCreateRequest(
            address_line1="123 Main St",
            city="Minneapolis",
            state="mn",  # should be uppercased
            postal_code="55401",
        )
        assert r.state == "MN"
        assert r.country == "US"
        assert r.geofence_radius_m == 150
        assert r.is_active is True
        assert r.latitude is None
        assert r.longitude is None

    def test_full_valid(self) -> None:
        r = LocationCreateRequest(
            label="Home",
            address_line1="123 Main St",
            address_line2="Apt 4B",
            city="Minneapolis",
            state="MN",
            postal_code="55401",
            country="US",
            latitude=Decimal("44.9778"),
            longitude=Decimal("-93.2650"),
            geofence_radius_m=200,
            is_active=True,
        )
        assert r.label == "Home"
        assert r.latitude == Decimal("44.9778")

    def test_empty_address_line1_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LocationCreateRequest(
                address_line1="",
                city="Minneapolis",
                state="MN",
                postal_code="55401",
            )

    def test_whitespace_only_address_line1_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LocationCreateRequest(
                address_line1="   ",
                city="Minneapolis",
                state="MN",
                postal_code="55401",
            )

    def test_state_wrong_length_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LocationCreateRequest(
                address_line1="123 Main St",
                city="Minneapolis",
                state="Minn",
                postal_code="55401",
            )

    def test_empty_postal_code_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LocationCreateRequest(
                address_line1="123 Main St",
                city="Minneapolis",
                state="MN",
                postal_code="",
            )

    def test_geofence_radius_too_small_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LocationCreateRequest(
                address_line1="123 Main St",
                city="Minneapolis",
                state="MN",
                postal_code="55401",
                geofence_radius_m=5,
            )

    def test_geofence_radius_too_large_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LocationCreateRequest(
                address_line1="123 Main St",
                city="Minneapolis",
                state="MN",
                postal_code="55401",
                geofence_radius_m=10_000,
            )


class TestLocationUpdateRequest:
    def test_all_optional(self) -> None:
        r = LocationUpdateRequest()
        dumped = r.model_dump(exclude_unset=True)
        assert dumped == {}

    def test_state_normalization(self) -> None:
        r = LocationUpdateRequest(state="ca")
        assert r.state == "CA"

    def test_partial_update(self) -> None:
        r = LocationUpdateRequest(label="New label", geofence_radius_m=300)
        dumped = r.model_dump(exclude_unset=True)
        assert dumped == {"label": "New label", "geofence_radius_m": 300}


class TestLocationResponse:
    def test_from_orm(self) -> None:
        loc = Location(
            id=uuid.uuid4(),
            agency_id=uuid.uuid4(),
            label="Home",
            address_line1="123 Main St",
            city="Minneapolis",
            state="MN",
            postal_code="55401",
            country="US",
            geofence_radius_m=150,
            is_active=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        r = LocationResponse.model_validate(loc)
        assert r.label == "Home"
        assert r.state == "MN"
        assert r.geofence_radius_m == 150
        assert r.is_active is True


class TestLocationListResponse:
    def test_envelope(self) -> None:
        # Empty list response
        r = LocationListResponse(
            data=[],
            pagination={"page": 1, "page_size": 20, "total": 0, "total_pages": 0},
        )
        assert r.data == []
        assert r.pagination.total == 0
        assert r.pagination.page == 1
