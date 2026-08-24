"""Unit tests for /portal/visits/* response schemas.

The portal is read-only — all state-mutating actions live on the
visits router (sign, confirm-billing). So the only schemas the
portal exposes are response shapes:
  - `PortalVisitListItem` — list endpoint
  - `PortalVisitResponse`  — single-visit detail

Per migration 0027 / spec alignment:
  - `service_items` field renamed to `activities`
  - `verification` field replaced by `signature` (AppointmentSignature)
  - `issues` field removed
  - New joined display fields (staff_name, patient_initials, etc.)
"""

from __future__ import annotations

import uuid

from src.modules.portal import schemas


# --------------------------------------------------------------------------
# Module surface
# --------------------------------------------------------------------------
def test_module_exports() -> None:
    """`PortalVisitListItem` + `PortalVisitResponse` exist."""
    assert hasattr(schemas, "PortalVisitListItem")
    assert hasattr(schemas, "PortalVisitResponse")


# --------------------------------------------------------------------------
# PortalVisitListItem
# --------------------------------------------------------------------------
class TestPortalVisitListItem:
    def test_minimal_instantiation(self) -> None:
        item = schemas.PortalVisitListItem.model_validate(
            {
                "id": str(uuid.uuid4()),
                "appointment_id": str(uuid.uuid4()),
                "status": "IN_PROGRESS",
                "scheduled_start": "2026-06-27T09:00:00Z",
                "scheduled_end": "2026-06-27T11:00:00Z",
                "duration_label": None,
                "staff_name": "John Smith",
                "created_at": "2026-06-27T08:30:00Z",
            }
        )
        assert item.status == "IN_PROGRESS"
        assert item.staff_name == "John Smith"
        assert item.duration_label is None

    def test_extra_fields_ignored(self) -> None:
        # `from_attributes=True` + extras=ignore default — Pydantic v2
        # drops unknown fields silently.
        item = schemas.PortalVisitListItem.model_validate(
            {
                "id": str(uuid.uuid4()),
                "appointment_id": str(uuid.uuid4()),
                "status": "SCHEDULED",
                "scheduled_start": "2026-06-27T09:00:00Z",
                "scheduled_end": "2026-06-27T11:00:00Z",
                "duration_label": None,
                "staff_name": None,
                "created_at": "2026-06-27T08:30:00Z",
                "extra_field": "should be ignored",
            }
        )
        assert item.status == "SCHEDULED"


# --------------------------------------------------------------------------
# PortalVisitResponse
# --------------------------------------------------------------------------
class TestPortalVisitResponse:
    def test_minimal_instantiation(self) -> None:
        resp = schemas.PortalVisitResponse.model_validate(
            {
                "id": str(uuid.uuid4()),
                "appointment_id": str(uuid.uuid4()),
                "agency_id": str(uuid.uuid4()),
                "staff_id": str(uuid.uuid4()),
                "status": "IN_PROGRESS",
                "billing_confirmed_at": None,
                "created_at": "2026-06-27T08:30:00Z",
                "updated_at": "2026-06-27T11:30:00Z",
            }
        )
        assert resp.status == "IN_PROGRESS"
        assert resp.billing_confirmed_at is None
        # New joined display fields default to None when not populated.
        assert resp.staff_name is None
        assert resp.patient_name is None
        assert resp.patient_initials is None
        assert resp.duration_label is None
        assert resp.time_range_label is None
        assert resp.visit_date_label is None
        assert resp.activities is None
        assert resp.notes is None
        assert resp.signature is None
        # Live GPS / sharing flag — defaults
        assert resp.live_lat is None
        assert resp.live_lng is None
        assert resp.live_ping_at is None
        assert resp.sharing_location is False