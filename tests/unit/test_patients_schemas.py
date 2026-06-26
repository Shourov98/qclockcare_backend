"""Unit tests for patients + guardians Pydantic schemas.

Pure-Pydantic: no DB, no app. Validates field-level constraints and the
custom model_validators (relationship date ordering, mutually-exclusive
guardian sources, etc.).
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest
from pydantic import ValidationError

from src.modules.patients.schemas import (
    GuardianProfileCreateRequest,
    GuardianProfileUpdateRequest,
    PatientGuardianRelationshipCreateRequest,
    PatientGuardianRelationshipUpdateRequest,
    PatientProfileCreateRequest,
    PatientProfileUpdateRequest,
)

_UUID_A = "00000000-0000-0000-0000-000000000001"


# --------------------------------------------------------------------------
# PatientProfileCreateRequest
# --------------------------------------------------------------------------
class TestPatientProfileCreateRequest:
    def test_minimal_required_fields(self) -> None:
        req = PatientProfileCreateRequest(
            email="p@example.com",
            full_name="Pat",
            patient_code="P-001",
        )
        assert req.email == "p@example.com"
        assert req.full_name == "Pat"
        assert req.patient_code == "P-001"
        assert req.date_of_birth is None
        assert req.admitted_at is None

    def test_all_fields(self) -> None:
        req = PatientProfileCreateRequest(
            email="bob@example.com",
            full_name="Bob",
            phone="+1-555-1234",
            patient_code="P-002",
            date_of_birth=date(1980, 5, 12),
            gender="male",
            preferred_language="en",
            admitted_at=date(2025, 1, 15),
        )
        assert req.date_of_birth == date(1980, 5, 12)
        assert req.preferred_language == "en"

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PatientProfileCreateRequest(
                email="x@example.com",
                full_name="X",
                patient_code="P-1",
                bogus="nope",  # type: ignore[call-arg]
            )

    def test_invalid_email_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PatientProfileCreateRequest(
                email="not-an-email", full_name="X", patient_code="P-1"
            )

    def test_patient_code_pattern(self) -> None:
        with pytest.raises(ValidationError):
            PatientProfileCreateRequest(
                email="x@example.com", full_name="X", patient_code="has spaces"
            )


class TestPatientProfileUpdateRequest:
    def test_all_optional(self) -> None:
        req = PatientProfileUpdateRequest()
        assert req.full_name is None
        assert req.status is None

    def test_partial(self) -> None:
        req = PatientProfileUpdateRequest(phone="555-0001", care_notes="note")
        assert req.phone == "555-0001"
        assert req.care_notes == "note"


# --------------------------------------------------------------------------
# Guardian schemas
# --------------------------------------------------------------------------
class TestGuardianProfileCreateRequest:
    def test_minimal(self) -> None:
        req = GuardianProfileCreateRequest(email="g@example.com", full_name="G")
        assert req.contact_phone is None
        assert req.contact_email is None

    def test_all_fields(self) -> None:
        req = GuardianProfileCreateRequest(
            email="g@example.com",
            full_name="G",
            phone="+1-555-9999",
            contact_phone="+1-555-8888",
            contact_email="contact@example.com",
            notes="Conservator",
        )
        assert req.contact_email == "contact@example.com"


class TestGuardianProfileUpdateRequest:
    def test_partial(self) -> None:
        req = GuardianProfileUpdateRequest(notes="Updated")
        assert req.notes == "Updated"
        assert req.contact_phone is None


# --------------------------------------------------------------------------
# Patient ↔ Guardian relationship
# --------------------------------------------------------------------------
class TestPatientGuardianRelationshipCreateRequest:
    def test_existing_guardian(self) -> None:
        req = PatientGuardianRelationshipCreateRequest(
            relationship_type="SPOUSE",
            guardian_id=UUID(_UUID_A),
        )
        assert req.relationship_type.value == "SPOUSE"
        assert req.is_legal is False

    def test_new_guardian(self) -> None:
        req = PatientGuardianRelationshipCreateRequest(
            relationship_type="PARENT",
            new_guardian=GuardianProfileCreateRequest(
                email="ng@example.com", full_name="NG"
            ),
            is_legal=True,
            valid_from=date(2025, 1, 1),
        )
        assert req.new_guardian.full_name == "NG"
        assert req.is_legal is True

    def test_both_sources_rejected(self) -> None:
        with pytest.raises(ValidationError) as ei:
            PatientGuardianRelationshipCreateRequest(
                relationship_type="SPOUSE",
                guardian_id=UUID(_UUID_A),
                new_guardian=GuardianProfileCreateRequest(
                    email="ng@example.com", full_name="NG"
                ),
            )
        assert "exactly one" in str(ei.value).lower()

    def test_no_source_rejected(self) -> None:
        with pytest.raises(ValidationError) as ei:
            PatientGuardianRelationshipCreateRequest(relationship_type="SPOUSE")
        assert "exactly one" in str(ei.value).lower()

    def test_invalid_date_range(self) -> None:
        with pytest.raises(ValidationError):
            PatientGuardianRelationshipCreateRequest(
                relationship_type="SPOUSE",
                guardian_id=UUID(_UUID_A),
                valid_from=date(2025, 6, 1),
                valid_until=date(2025, 1, 1),
            )

    def test_dates_equal_ok(self) -> None:
        d = date(2025, 6, 1)
        req = PatientGuardianRelationshipCreateRequest(
            relationship_type="SPOUSE",
            guardian_id=UUID(_UUID_A),
            valid_from=d,
            valid_until=d,
        )
        assert req.valid_until == d


class TestPatientGuardianRelationshipUpdateRequest:
    def test_all_optional(self) -> None:
        req = PatientGuardianRelationshipUpdateRequest()
        assert req.is_legal is None

    def test_partial(self) -> None:
        req = PatientGuardianRelationshipUpdateRequest(
            is_legal=True, valid_until=date(2030, 1, 1)
        )
        assert req.is_legal is True
        assert req.valid_until == date(2030, 1, 1)

    def test_invalid_date_range(self) -> None:
        with pytest.raises(ValidationError):
            PatientGuardianRelationshipUpdateRequest(
                valid_from=date(2025, 6, 1),
                valid_until=date(2025, 1, 1),
            )