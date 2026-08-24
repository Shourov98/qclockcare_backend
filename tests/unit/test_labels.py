"""Unit tests for the shared `src.shared.utils.labels` helpers.

Pure-Python — verifies the display-label formatters used by the
appointments, visits, and portal modules render exactly as the
QlockCare spec dictates.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.shared.utils.labels import (
    duration_label,
    humanize_enum,
    patient_initials,
    signer_display_name,
    time_of_day_label,
    time_range_label,
    visit_date_label,
)


# --------------------------------------------------------------------------
# humanize_enum
# --------------------------------------------------------------------------
class TestHumanizeEnum:
    def test_none_returns_none(self) -> None:
        assert humanize_enum(None) is None

    def test_personal_care(self) -> None:
        from src.shared.domain.enums import ServiceType

        assert humanize_enum(ServiceType.PERSONAL_CARE) == "Personal Care"

    def test_acronym_first_letter_capitalised(self) -> None:
        # humanize_enum's contract is title-casing each underscore-separated
        # segment (capitalize() in Python). For an all-alpha segment like
        # "CFSS" this gives "Cfss" — the function doesn't detect acronyms.
        # If we ever want acronym detection (e.g. "CFSS" -> "CFSS"), add
        # it as a separate helper; this is just the baseline.
        from src.shared.domain.enums import ProgramType

        assert humanize_enum(ProgramType.CFSS) == "Cfss"

    def test_appointment_status(self) -> None:
        from src.shared.domain.enums import AppointmentStatus

        assert (
            humanize_enum(AppointmentStatus.AWAITING_SIGNATURE)
            == "Awaiting Signature"
        )

    def test_underscore_separated(self) -> None:
        assert humanize_enum("SOMETHING_LIKE_THIS") == "Something Like This"

    def test_digit_segment_preserved(self) -> None:
        # Spec example: program "245D" should not become "245 D"
        assert humanize_enum("245D") == "245D"


# --------------------------------------------------------------------------
# patient_initials
# --------------------------------------------------------------------------
class TestPatientInitials:
    def test_two_part_name(self) -> None:
        assert patient_initials("John Smith") == "JS"

    def test_three_part_name_uses_first_and_last(self) -> None:
        assert patient_initials("Sarah Jane Ahmed") == "SA"

    def test_single_name_collapses_to_two_letters(self) -> None:
        assert patient_initials("Madonna") == "MA"

    def test_extra_whitespace_ignored(self) -> None:
        assert patient_initials("  John   Smith  ") == "JS"

    def test_none_returns_none(self) -> None:
        assert patient_initials(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert patient_initials("") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert patient_initials("   ") is None


# --------------------------------------------------------------------------
# signer_display_name
# --------------------------------------------------------------------------
class TestSignerDisplayName:
    def test_two_part_name_per_spec(self) -> None:
        assert signer_display_name("John Smith") == "J. Smith"

    def test_three_part_name(self) -> None:
        # Spec rule is first-letter-of-first + last-name
        assert signer_display_name("Sarah Jane Ahmed") == "S. Ahmed"

    def test_single_name_stays_as_is(self) -> None:
        assert signer_display_name("Madonna") == "Madonna"

    def test_none_returns_none(self) -> None:
        assert signer_display_name(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert signer_display_name("") is None

    def test_extra_whitespace_ignored(self) -> None:
        assert signer_display_name("  John  Smith  ") == "J. Smith"


# --------------------------------------------------------------------------
# duration_label
# --------------------------------------------------------------------------
class TestDurationLabel:
    def test_none_returns_none(self) -> None:
        assert duration_label(None) is None

    def test_hours_and_minutes(self) -> None:
        # 7500 seconds = 2h 05m
        assert duration_label(7500) == "2h 05m"

    def test_one_hour_exactly(self) -> None:
        assert duration_label(3600) == "1h 00m"

    def test_under_one_hour_drops_h_prefix(self) -> None:
        # 2700 seconds = 45m (under an hour)
        assert duration_label(2700) == "45m"

    def test_one_minute(self) -> None:
        assert duration_label(60) == "1m"

    def test_zero_seconds(self) -> None:
        assert duration_label(0) == "0m"

    def test_negative_seconds_clamped_to_zero(self) -> None:
        assert duration_label(-5) == "0m"


# --------------------------------------------------------------------------
# time_of_day_label
# --------------------------------------------------------------------------
class TestTimeOfDayLabel:
    def test_morning(self) -> None:
        assert time_of_day_label(datetime(2026, 5, 5, 9, 2)) == "9:02 AM"

    def test_afternoon(self) -> None:
        assert time_of_day_label(datetime(2026, 5, 5, 14, 30)) == "2:30 PM"

    def test_noon(self) -> None:
        assert time_of_day_label(datetime(2026, 5, 5, 12, 0)) == "12:00 PM"

    def test_midnight(self) -> None:
        assert time_of_day_label(datetime(2026, 5, 5, 0, 0)) == "12:00 AM"

    def test_none_returns_none(self) -> None:
        assert time_of_day_label(None) is None


# --------------------------------------------------------------------------
# visit_date_label
# --------------------------------------------------------------------------
class TestVisitDateLabel:
    def test_known_date(self) -> None:
        # 2026-05-05 was a Tuesday
        assert visit_date_label(datetime(2026, 5, 5, 9, 2)) == "Tuesday, May 5, 2026"

    def test_first_of_month(self) -> None:
        # 2026-07-01 was a Wednesday
        assert (
            visit_date_label(datetime(2026, 7, 1, 9, 0)) == "Wednesday, July 1, 2026"
        )

    def test_none_returns_none(self) -> None:
        assert visit_date_label(None) is None


# --------------------------------------------------------------------------
# time_range_label
# --------------------------------------------------------------------------
class TestTimeRangeLabel:
    def test_full_range_uses_em_dash(self) -> None:
        start = datetime(2026, 5, 5, 9, 2)
        end = datetime(2026, 5, 5, 11, 7)
        # Em-dash, not hyphen — important per spec mockup
        assert time_range_label(start, end) == "9:02 AM \u2014 11:07 AM"

    def test_only_start(self) -> None:
        start = datetime(2026, 5, 5, 9, 2)
        assert time_range_label(start, None) == "9:02 AM"

    def test_only_end(self) -> None:
        end = datetime(2026, 5, 5, 11, 7)
        assert time_range_label(None, end) == "11:07 AM"

    def test_both_none(self) -> None:
        assert time_range_label(None, None) is None

    def test_tz_aware_passes_through(self) -> None:
        # The helper uses strftime which honours the input tz if present,
        # but produces a 12-hour clock label either way.
        start = datetime(2026, 5, 5, 9, 2, tzinfo=timezone.utc)
        end = datetime(2026, 5, 5, 11, 7, tzinfo=timezone.utc)
        assert time_range_label(start, end) == "9:02 AM \u2014 11:07 AM"
