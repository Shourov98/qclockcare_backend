"""Datetime utilities — always UTC, always aware.

Never use `datetime.now()` without a tz. Always use `utc_now()` so timestamps
are unambiguous regardless of the host timezone (containers, dev laptops, etc.).
"""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Timezone-aware current UTC time.

    Returns:
        datetime with tzinfo=UTC, e.g. `2026-06-27 14:30:00+00:00`.

    All DB columns are `timestamptz`, which Postgres stores in UTC regardless.
    """
    return datetime.now(tz=UTC)


def utc_now_naive() -> datetime:
    """Naive UTC datetime — only for places that strip tzinfo (legacy code, tests).

    Prefer `utc_now()` for anything new.
    """
    return datetime.now(tz=UTC).replace(tzinfo=None)
