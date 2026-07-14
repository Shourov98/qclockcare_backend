"""Unit tests for /portal/visits/* request schemas."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from src.modules.portal.schemas import (
    PortalDisputeRequest,
    PortalReportIssueRequest,
    PortalVerifyRequest,
)


# --------------------------------------------------------------------------
# PortalVerifyRequest
# --------------------------------------------------------------------------
class TestPortalVerifyRequest:
    def test_minimal_ok(self) -> None:
        v = PortalVerifyRequest()
        assert v.comment is None

    def test_with_comment_ok(self) -> None:
        v = PortalVerifyRequest(comment="thanks!")
        assert v.comment == "thanks!"

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PortalVerifyRequest(status="VERIFIED")  # type: ignore[call-arg]


# --------------------------------------------------------------------------
# PortalDisputeRequest
# --------------------------------------------------------------------------
class TestPortalDisputeRequest:
    def test_minimal_ok(self) -> None:
        v = PortalDisputeRequest(dispute_reason_code="SERVICE_NOT_RECEIVED")
        assert v.dispute_reason_code == "SERVICE_NOT_RECEIVED"
        assert v.comment is None

    def test_missing_reason_code_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PortalDisputeRequest()  # type: ignore[call-arg]

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PortalDisputeRequest(dispute_reason_code="X", status="DISPUTED")  # type: ignore[call-arg]


# --------------------------------------------------------------------------
# PortalReportIssueRequest
# --------------------------------------------------------------------------
class TestPortalReportIssueRequest:
    def test_minimal_ok(self) -> None:
        v = PortalReportIssueRequest(
            issue_type="noise_complaint",
            comment="Too loud.",
        )
        assert v.issue_type == "noise_complaint"
        assert v.comment == "Too loud."

    def test_empty_issue_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PortalReportIssueRequest(issue_type="", comment="hello")

    def test_empty_comment_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PortalReportIssueRequest(issue_type="noise", comment="")

    def test_overlong_issue_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PortalReportIssueRequest(
                issue_type="x" * 300,
                comment="ok",
            )

    def test_overlong_comment_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PortalReportIssueRequest(
                issue_type="noise",
                comment="x" * 5000,
            )

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PortalReportIssueRequest(
                issue_type="noise",
                comment="ok",
                severity="high",  # type: ignore[call-arg]
            )


# --------------------------------------------------------------------------
# Module surface
# --------------------------------------------------------------------------
def test_module_exports() -> None:
    """PortalVisitListItem etc. exist (response schemas — light coverage)."""
    from src.modules.portal import schemas

    assert hasattr(schemas, "PortalVisitListItem")
    assert hasattr(schemas, "PortalVisitResponse")
    # Just ensure they instantiate the bare shape.
    item = schemas.PortalVisitListItem.model_validate(
        {
            "id": str(uuid.uuid4()),
            "appointment_id": str(uuid.uuid4()),
            "status": "CHECKED_IN",
            "check_in_time": None,
            "check_out_time": None,
            "duration_seconds": None,
            "created_at": "2026-06-27T10:00:00Z",
        }
    )
    assert item.status == "CHECKED_IN"
