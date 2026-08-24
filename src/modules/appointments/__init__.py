"""Appointments module — scheduled visits linking patients ↔ staff.

This module covers the operational core: a scheduled visit by a staff
member for a patient at an agency, with a list of activities to deliver
during that visit. The 5-state lifecycle per the spec flows through
the appointment status.

Tables:
- `appointments`                  — the scheduled visit
- `appointment_activities`        — free-text line items: each activity to deliver

All tables are agency-scoped. RLS policies are defined in migration 0027.

See `QlockCare_appointemnt_flow.md` for the canonical spec and the
status lifecycle.
"""

from __future__ import annotations

__all__: list[str] = []
