"""Patients module — care-recipient profiles, guardians, relationships.

This module covers the care-recipient side of the platform. A `User` can
hold a patient profile at one agency and a guardian profile at the same
(or different) agency. The relationship table links the two with
relationship metadata (spouse, conservator, etc.) and a legal-authority flag.

Tables:
- `patient_profiles`                — per-agency care-recipient record
- `guardian_profiles`               — per-agency authorised person
- `patient_guardian_relationships`  — many-to-many patient ↔ guardian

All three are agency-scoped; RLS policies are defined in migration 0005.

See `13_DATABASE_SCHEMA_COMPLETE.md` §7 for the full data model.
"""

from __future__ import annotations

__all__: list[str] = []