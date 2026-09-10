"""DTOs for agency operational compliance workflows."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.shared.domain.enums import ComplianceIssueCategory, ComplianceIssueSeverity


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServiceAuthorizationCreateRequest(_Strict):
    patient_id: uuid.UUID
    program_type: str = Field(min_length=1, max_length=64)
    service_name: str = Field(min_length=1, max_length=255)
    authorization_number: str | None = Field(default=None, max_length=128)
    starts_on: date
    ends_on: date
    authorized_units: float = Field(gt=0)
    unit_label: str = Field(default="hours", min_length=1, max_length=32)
    notes: str | None = Field(default=None, max_length=4000)


class ServiceAuthorizationUsageRequest(_Strict):
    units: float = Field(gt=0)


class ServiceAuthorizationResponse(_Strict):
    id: uuid.UUID
    patient_id: uuid.UUID
    patient_name: str
    program_type: str
    service_name: str
    authorization_number: str | None
    starts_on: date
    ends_on: date
    authorized_units: float
    used_units: float
    remaining_units: float
    unit_label: str
    status: str
    notes: str | None
    created_at: datetime


class SupervisoryVisitCreateRequest(_Strict):
    staff_id: uuid.UUID
    patient_id: uuid.UUID | None = None
    scheduled_at: datetime
    objectives: str | None = Field(default=None, max_length=4000)


class SupervisoryVisitCompleteRequest(_Strict):
    findings: str = Field(min_length=1, max_length=4000)

    @field_validator("findings")
    @classmethod
    def strip_findings(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class SupervisoryVisitResponse(_Strict):
    id: uuid.UUID
    staff_id: uuid.UUID
    staff_name: str
    patient_id: uuid.UUID | None
    patient_name: str | None
    supervisor_name: str
    scheduled_at: datetime
    completed_at: datetime | None
    status: str
    objectives: str | None
    findings: str | None
    acknowledged_at: datetime | None


class ComplianceReportCreateRequest(_Strict):
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=4000)
    category: ComplianceIssueCategory = ComplianceIssueCategory.OTHER
    severity: ComplianceIssueSeverity = ComplianceIssueSeverity.MEDIUM
    patient_id: uuid.UUID | None = None

    @field_validator("title", "description")
    @classmethod
    def strip_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class ComplianceReportResolveRequest(_Strict):
    resolution_note: str = Field(min_length=1, max_length=4000)
    dismiss: bool = False


class ComplianceReportResponse(_Strict):
    id: uuid.UUID
    reporter_name: str
    patient_id: uuid.UUID | None
    patient_name: str | None
    category: ComplianceIssueCategory
    severity: ComplianceIssueSeverity
    status: str
    title: str
    description: str
    resolution_note: str | None
    created_at: datetime
    resolved_at: datetime | None


class ComplianceOverviewResponse(_Strict):
    authorizations_active: int
    authorizations_expiring: int
    authorizations_expired: int
    supervisory_due: int
    supervisory_completed_this_month: int
    reports_open: int
    reports_critical: int
