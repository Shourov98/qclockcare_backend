"""Compliance Pydantic schemas — request / response DTOs.

Mirrors `compliance.models` and the routes documented in
`compliance/__init__.py`. All schemas use `extra="forbid"` so unknown
fields produce a 422.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.shared.domain.enums import DocumentStatus, DocumentType, LicenseStatus


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------
class AgencyDocumentCreateRequest(BaseModel):
    """Create a required-document record for an agency."""

    model_config = ConfigDict(extra="forbid")

    agency_id: uuid.UUID
    name: str = Field(min_length=1, max_length=255)
    doc_type: DocumentType = DocumentType.DOCUMENT
    status: DocumentStatus = DocumentStatus.MISSING
    description: str | None = Field(default=None, max_length=2000)
    expires_at: datetime | None = None
    file_url: str | None = Field(default=None, max_length=1024)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty or whitespace-only")
        return stripped


class AgencyDocumentUpdateRequest(BaseModel):
    """Partial update of a document record."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    doc_type: DocumentType | None = None
    status: DocumentStatus | None = None
    description: str | None = Field(default=None, max_length=2000)
    expires_at: datetime | None = None
    file_url: str | None = Field(default=None, max_length=1024)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty or whitespace-only")
        return stripped


class AgencyDocumentResponse(BaseModel):
    """One document record."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agency_id: uuid.UUID
    name: str
    doc_type: DocumentType
    status: DocumentStatus
    description: str | None
    expires_at: datetime | None
    file_url: str | None
    extra: dict
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


# --------------------------------------------------------------------------
# Missing documents report
# --------------------------------------------------------------------------
class MissingDocumentsRowResponse(BaseModel):
    """One row of the per-agency missing-documents report.

    `documents` is the slice of `MISSING` documents (paginated by the FE).
    The response is intentionally flat so the dashboard doesn't need a
    second roundtrip.
    """

    model_config = ConfigDict(extra="forbid")

    agency_id: uuid.UUID
    agency_name: str
    missing_count: int
    # Worst severity for this agency (CRITICAL / WARNING / UPCOMING) —
    # used to colour-code the row in the FE.
    worst_status: DocumentStatus | None = None
    documents: list[AgencyDocumentResponse]


# --------------------------------------------------------------------------
# Licenses
# --------------------------------------------------------------------------
class AgencyLicenseCreateRequest(BaseModel):
    """Create a license record for an agency."""

    model_config = ConfigDict(extra="forbid")

    agency_id: uuid.UUID
    name: str = Field(min_length=1, max_length=255)
    doc_type: DocumentType = DocumentType.LICENSE
    status: LicenseStatus | None = None  # computed if absent
    issued_at: datetime | None = None
    expires_at: datetime
    reference_number: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty or whitespace-only")
        return stripped


class AgencyLicenseUpdateRequest(BaseModel):
    """Partial update of a license record."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    doc_type: DocumentType | None = None
    status: LicenseStatus | None = None
    issued_at: datetime | None = None
    expires_at: datetime | None = None
    reference_number: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty or whitespace-only")
        return stripped


class AgencyLicenseResponse(BaseModel):
    """One license record."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agency_id: uuid.UUID
    name: str
    doc_type: DocumentType
    status: LicenseStatus
    issued_at: datetime | None
    expires_at: datetime
    reference_number: str | None
    notes: str | None
    extra: dict
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


# --------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------
class ComplianceStatsResponse(BaseModel):
    """Aggregate counts for the dashboard summary cards."""

    model_config = ConfigDict(extra="forbid")

    # Documents
    documents_total: int
    documents_missing: int
    documents_expiring: int
    documents_expired: int
    documents_valid: int

    # Licenses — counts for each bucket the FE cares about.
    licenses_total: int
    licenses_critical: int
    licenses_warning: int
    licenses_upcoming: int
    licenses_valid: int
    licenses_expired: int


# --------------------------------------------------------------------------
# List responses (offset paginated)
# --------------------------------------------------------------------------
class DocumentListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: list[AgencyDocumentResponse]
    pagination: "OffsetPaginationMeta"


class LicenseListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: list[AgencyLicenseResponse]
    pagination: "OffsetPaginationMeta"


class OffsetPaginationMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int
    page_size: int
    total: int
    total_pages: int


__all__ = [
    "AgencyDocumentCreateRequest",
    "AgencyDocumentResponse",
    "AgencyDocumentUpdateRequest",
    "AgencyLicenseCreateRequest",
    "AgencyLicenseResponse",
    "AgencyLicenseUpdateRequest",
    "ComplianceStatsResponse",
    "DocumentListResponse",
    "LicenseListResponse",
    "MissingDocumentsRowResponse",
    "OffsetPaginationMeta",
]
