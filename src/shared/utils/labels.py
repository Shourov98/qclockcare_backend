"""Shared display-label helpers.

Pure functions that turn raw DB values into the human-readable strings the
mobile/web clients render. Lives in `shared.utils` so both the
appointments module and the visits module can import without creating a
circular dependency.

Conventions:
- Every helper accepts `None` and returns `None` so callers can chain
  them without guarding for empty values.
- Date/time formatters render in the agency's local timezone when the
  caller passes a tz-aware datetime; naive datetimes are assumed UTC.
- The 12-hour clock is used everywhere on the patient/staff app
  (`9:02 AM` not `09:02`).

Spec reference: `QlockCare_appointemnt_flow.md` §9 (signature display
name format) and §10 (EVV record display).
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

# 12-hour minute format with leading zero. The mobile app uses this
# almost everywhere ("9:02 AM", "10:24 AM").
_TIME_OF_DAY_FMT: Final[str] = "%-I:%M %p"
# Full date with weekday. Used on the visit summary header card.
_VISIT_DATE_FMT: Final[str] = "%A, %B %-d, %Y"
# Em-dash separator for time ranges. The mobile app renders "9:02 AM —
# 11:07 AM" with a real em-dash, not a hyphen.
_RANGE_SEPARATOR: Final[str] = " \u2014 "  # em-dash
# Hours/minutes component separator for durations.
_DURATION_HM_SEP: Final[str] = "h "
_DURATION_MIN_SUFFIX: Final[str] = "m"


def humanize_enum(value: object) -> str | None:
    """Render a `StrEnum` value as a human-readable label.

    Examples:
        `ServiceType.PERSONAL_CARE` -> `"Personal Care"`
        `ProgramType.CFSS` -> `"CFSS"` (acronyms are kept uppercase)
        `AppointmentStatus.AWAITING_SIGNATURE` -> `"Awaiting Signature"`
        `None` -> `None`

    Uses the enum's `.value` so the wire format of the underlying
    constants stays the source of truth (no separate label table).
    """
    if value is None:
        return None
    raw = getattr(value, "value", str(value))
    # Title-case each underscore-separated segment, but leave segments
    # that are pure digits (e.g. "245D") untouched.
    return " ".join(
        seg.capitalize() if seg.isalpha() else seg for seg in raw.split("_")
    )


def patient_initials(full_name: str | None) -> str | None:
    """Return the patient's avatar initials.

    Examples:
        `"John Smith"` -> `"JS"`
        `"Sarah"` -> `"SA"`
        `None` -> `None`
        `""` -> `None`

    Returns at most 2 letters, uppercase. Single-name inputs collapse to
    the first two letters (rare, but defensive — the DB doesn't enforce
    a last-name field).
    """
    if not full_name:
        return None
    parts = [p for p in full_name.strip().split() if p]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def signer_display_name(full_name: str | None) -> str | None:
    """Format a name as a signature display name per the spec.

    Spec rule (QlockCare_appointemnt_flow.md §9):
        First letter of first name + "." + last name
    Examples:
        `"John Smith"` -> `"J. Smith"`
        `"Sarah Ahmed"` -> `"S. Ahmed"`
        `"Madonna"` -> `"Madonna"` (single-name inputs stay as-is)
        `None` -> `None`

    Used by the signature endpoint to populate `AppointmentSignature.signer_display_name`.
    """
    if not full_name:
        return None
    parts = [p for p in full_name.strip().split() if p]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0][0]}. {parts[-1]}"


def duration_label(seconds: int | None) -> str | None:
    """Format a duration in seconds as `"Xh YYm"`.

    Examples:
        `7500` -> `"2h 05m"`
        `3600` -> `"1h 00m"`
        `2700` -> `"45m"` (under an hour — drop the `0h` prefix)
        `60` -> `"1m"`
        `None` -> `None`

    Spec §1 says duration is auto-computed from end − start; this
    function renders the result.
    """
    if seconds is None:
        return None
    if seconds < 0:
        seconds = 0
    hours, rem = divmod(int(seconds), 3600)
    minutes = rem // 60
    if hours == 0:
        return f"{minutes}{_DURATION_MIN_SUFFIX}"
    return f"{hours}{_DURATION_HM_SEP}{minutes:02d}{_DURATION_MIN_SUFFIX}"


def time_of_day_label(dt: datetime | None) -> str | None:
    """Format a datetime as `"9:02 AM"` (12-hour, no leading zero on hour).

    Examples:
        `datetime(2026, 5, 5, 9, 2)` -> `"9:02 AM"`
        `datetime(2026, 5, 5, 14, 30)` -> `"2:30 PM"`
        `None` -> `None`

    Used by visit notes (timestamp), activities (completed_at), and the
    EVV record (start/end).
    """
    if dt is None:
        return None
    return dt.strftime(_TIME_OF_DAY_FMT)


def visit_date_label(dt: datetime | None) -> str | None:
    """Format a datetime as `"Tuesday, May 5, 2026"`.

    Examples:
        `datetime(2026, 5, 5, 9, 2)` -> `"Tuesday, May 5, 2026"`
        `None` -> `None`

    Used on the visit summary header card.
    """
    if dt is None:
        return None
    return dt.strftime(_VISIT_DATE_FMT)


def time_range_label(
    start: datetime | None, end: datetime | None
) -> str | None:
    """Format a start/end pair as `"9:02 AM — 11:07 AM"`.

    Examples:
        `(9:02, 11:07)` -> `"9:02 AM — 11:07 AM"`
        `(9:02, None)` -> `"9:02 AM"` (single bound)
        `(None, None)` -> `None`

    Em-dash separator matches the spec's Visit Summary mockup.
    """
    s = time_of_day_label(start)
    e = time_of_day_label(end)
    if s and e:
        return f"{s}{_RANGE_SEPARATOR}{e}"
    return s or e


__all__ = [
    "duration_label",
    "humanize_enum",
    "patient_initials",
    "signer_display_name",
    "time_of_day_label",
    "time_range_label",
    "visit_date_label",
]
