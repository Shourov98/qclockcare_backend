"""Appointments module — scheduled visits linking patients ↔ staff.

This module covers the operational core: a scheduled visit by a staff
member for a patient at an agency, with a list of services to deliver
during that visit. Confirmation, check-in/check-out, completion, and
cancellation all flow through the appointment status.

Tables:
- `appointments`                  — the scheduled visit
- `appointment_service_items`     — line items: each service to deliver

All tables are agency-scoped. RLS policies are defined in migration 0006.

See `13_DATABASE_SCHEMA_COMPLETE.md` §8 for the data model and the
status lifecycle.
"""

from __future__ import annotations

__all__: list[str] = []
