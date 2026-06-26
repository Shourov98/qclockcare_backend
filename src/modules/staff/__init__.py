"""Staff module — agency staff profiles, qualifications, and availability.

This module covers the care-provider side of the platform: who works for an
agency, what credentials they hold, and when they're free. It is the first
end-user-facing surface (after identity) — agencies create staff via
invitation, staff complete onboarding, and over time agencies add/renew
qualifications and curate availability windows.

Tables:
- `staff_profiles`         — one per agency staff member (links to users)
- `staff_qualifications`   — credentials held by a staff member
- `staff_availability`     — recurring weekly windows + one-off blocks

All three are agency-scoped and RLS-protected.

See `13_DATABASE_SCHEMA_COMPLETE.md` §6 for the full data model.
"""

from __future__ import annotations

__all__: list[str] = []
