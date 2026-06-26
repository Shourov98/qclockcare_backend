"""Unit tests for staff Pydantic schemas.

Pure-Pydantic: no DB, no app. Validates field-level constraints and the
custom model validators on availability/qualification requests.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone

import pytest
from pydantic import ValidationError

from src.modules.staff.schemas import (
    StaffAvailabilityCreateRequest,
    StaffAvailabilityUpdateRequest,
    StaffProfileCreateRequest,
    StaffProfileUpdateRequest,
    StaffQualificationCreateRequest,
    StaffQualificationUpdateRequest,
)


# --------------------------------------------------------------------------
# StaffProfileCreateRequest
# --------------------------------------------------------------------------
class TestStaffProfileCreateRequest:
    def test_minimal_required_fields(self) -> None:
        req = StaffProfileCreateRequest(
            email="alice@example.com",
            full_name="Alice",
            staff_code="STF-001",
        )
        assert req.email == "alice@example.com"
        assert req.full_name == "Alice"
        assert req.staff_code == "STF-001"
        assert req.hired_at is None
        assert req.phone is None

    def test_all_fields(self) -> None:
        req = StaffProfileCreateRequest(
            email="bob@example.com",
            full_name="Bob Roberts",
            phone="+1-555-1234",
            staff_code="STF-002",
            hired_at=date(2025, 1, 15),
        )
        assert req.phone == "+1-555-1234"
        assert req.hired_at == date(2025, 1, 15)

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            StaffProfileCreateRequest(
                email="x@example.com",
                full_name="X",
                staff_code="STF-1",
                bogus="nope",  # type: ignore[call-arg]
            )

    def test_short_staff_code_rejected(self) -> None:
        with pytest.raises(ValidationError):
            StaffProfileCreateRequest(
                email="x@example.com",
                full_name="X",
                staff_code="",
            )

    def test_staff_code_pattern_enforced(self) -> None:
        with pytest.raises(ValidationError):
            StaffProfileCreateRequest(
                email="x@example.com",
                full_name="X",
                staff_code="has spaces",
            )

    def test_invalid_email_rejected(self) -> None:
        with pytest.raises(ValidationError):
            StaffProfileCreateRequest(
                email="not-an-email",
                full_name="X",
                staff_code="STF-1",
            )


# --------------------------------------------------------------------------
# StaffProfileUpdateRequest
# --------------------------------------------------------------------------
class TestStaffProfileUpdateRequest:
    def test_all_optional(self) -> None:
        req = StaffProfileUpdateRequest()
        assert req.full_name is None
        assert req.staff_code is None
        assert req.status is None

    def test_partial_update(self) -> None:
        req = StaffProfileUpdateRequest(phone="555-0001")
        assert req.phone == "555-0001"
        assert req.full_name is None


# --------------------------------------------------------------------------
# StaffQualificationCreateRequest
# --------------------------------------------------------------------------
class TestStaffQualificationCreateRequest:
    def test_minimal(self) -> None:
        req = StaffQualificationCreateRequest(qualification_type="CPR")
        assert req.qualification_type.value == "CPR"
        assert req.program_type is None
        # default status
        from src.shared.domain.enums import QualificationStatus
        assert req.status == QualificationStatus.PENDING_VERIFICATION

    def test_dates_must_be_ordered(self) -> None:
        with pytest.raises(ValidationError) as ei:
            StaffQualificationCreateRequest(
                qualification_type="CPR",
                issued_at=date(2025, 6, 1),
                expires_at=date(2025, 1, 1),
            )
        assert "expires_at" in str(ei.value)

    def test_dates_equal_ok(self) -> None:
        d = date(2025, 6, 1)
        req = StaffQualificationCreateRequest(
            qualification_type="FIRST_AID",
            issued_at=d,
            expires_at=d,
        )
        assert req.expires_at == d

    def test_program_type_optional(self) -> None:
        req = StaffQualificationCreateRequest(
            qualification_type="ARMHS_PROVIDER",
            program_type="ARMHS",
        )
        assert req.program_type.value == "ARMHS"


# --------------------------------------------------------------------------
# StaffAvailabilityCreateRequest
# --------------------------------------------------------------------------
class TestStaffAvailabilityCreateRequest:
    def test_recurring_window(self) -> None:
        req = StaffAvailabilityCreateRequest(
            day_of_week=0,
            start_time=time(8, 0),
            end_time=time(12, 0),
        )
        assert req.day_of_week == 0
        assert req.is_unavailable is False

    def test_one_off_window(self) -> None:
        req = StaffAvailabilityCreateRequest(
            specific_date=date(2025, 6, 27),
            reason="vacation",
        )
        assert req.specific_date == date(2025, 6, 27)

    def test_one_off_with_time_range(self) -> None:
        req = StaffAvailabilityCreateRequest(
            specific_date=date(2025, 6, 27),
            specific_start=datetime(2025, 6, 27, 9, 0, tzinfo=timezone.utc),
            specific_end=datetime(2025, 6, 27, 17, 0, tzinfo=timezone.utc),
        )
        assert req.specific_end > req.specific_start

    def test_both_flavours_rejected(self) -> None:
        with pytest.raises(ValidationError) as ei:
            StaffAvailabilityCreateRequest(
                day_of_week=0,
                start_time=time(8, 0),
                end_time=time(12, 0),
                specific_date=date(2025, 6, 27),
            )
        assert "exactly one" in str(ei.value).lower()

    def test_no_flavour_rejected(self) -> None:
        with pytest.raises(ValidationError) as ei:
            StaffAvailabilityCreateRequest(reason="nothing to do")
        assert "exactly one" in str(ei.value).lower()

    def test_recurring_without_times_rejected(self) -> None:
        with pytest.raises(ValidationError):
            StaffAvailabilityCreateRequest(day_of_week=0)

    def test_recurring_end_before_start_rejected(self) -> None:
        with pytest.raises(ValidationError):
            StaffAvailabilityCreateRequest(
                day_of_week=0,
                start_time=time(12, 0),
                end_time=time(8, 0),
            )

    def test_recurring_end_equal_start_rejected(self) -> None:
        with pytest.raises(ValidationError):
            StaffAvailabilityCreateRequest(
                day_of_week=0,
                start_time=time(8, 0),
                end_time=time(8, 0),
            )

    def test_one_off_end_before_start_rejected(self) -> None:
        with pytest.raises(ValidationError):
            StaffAvailabilityCreateRequest(
                specific_date=date(2025, 6, 27),
                specific_start=datetime(2025, 6, 27, 17, 0, tzinfo=timezone.utc),
                specific_end=datetime(2025, 6, 27, 9, 0, tzinfo=timezone.utc),
            )

    def test_day_of_week_bounds(self) -> None:
        # 0..6 inclusive
        for d in (0, 1, 2, 3, 4, 5, 6):
            StaffAvailabilityCreateRequest(
                day_of_week=d,
                start_time=time(8, 0),
                end_time=time(12, 0),
            )
        for bad in (-1, 7, 100):
            with pytest.raises(ValidationError):
                StaffAvailabilityCreateRequest(
                    day_of_week=bad,
                    start_time=time(8, 0),
                    end_time=time(12, 0),
                )

    def test_is_unavailable_default_false(self) -> None:
        req = StaffAvailabilityCreateRequest(
            day_of_week=0, start_time=time(8, 0), end_time=time(12, 0)
        )
        assert req.is_unavailable is False


# --------------------------------------------------------------------------
# StaffAvailabilityUpdateRequest
# --------------------------------------------------------------------------
class TestStaffAvailabilityUpdateRequest:
    def test_all_optional(self) -> None:
        req = StaffAvailabilityUpdateRequest()
        assert req.is_unavailable is None
        assert req.reason is None

    def test_flip_unavailable(self) -> None:
        req = StaffAvailabilityUpdateRequest(is_unavailable=True, reason="out")
        assert req.is_unavailable is True
        assert req.reason == "out"
