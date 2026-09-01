"""Audit log response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.shared.domain.enums import AuditAction


class AuditLogResponse(BaseModel):
    """Single audit log entry — read-only shape."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    agency_id: UUID | None
    actor_user_id: UUID | None
    action: AuditAction
    entity_type: str
    entity_id: UUID | None
    old_data: dict[str, Any] | None = None
    new_data: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    ip_address: str | None = None
    user_agent: str | None = None
    created_at: datetime

    @classmethod
    def model_validate(cls, obj):  # type: ignore[override]
        """Map ORM `metadata_` attribute to JSON `metadata` field."""
        if hasattr(obj, "metadata_"):
            data = {
                "id": obj.id,
                "agency_id": obj.agency_id,
                "actor_user_id": obj.actor_user_id,
                "action": obj.action,
                "entity_type": obj.entity_type,
                "entity_id": obj.entity_id,
                "old_data": obj.old_data,
                "new_data": obj.new_data,
                "metadata": obj.metadata_,
                "ip_address": str(obj.ip_address) if obj.ip_address else None,
                "user_agent": obj.user_agent,
                "created_at": obj.created_at,
            }
            return super().model_validate(data)
        return super().model_validate(obj)


# --------------------------------------------------------------------------
# Filter dropdown values (powers the FE audit-log page filter selects)
# --------------------------------------------------------------------------
class AuditLogActorSummary(BaseModel):
    """One distinct actor that has produced audit rows in the caller's scope."""

    model_config = ConfigDict(from_attributes=True)

    user_id: UUID
    full_name: str | None = None
    email: str | None = None
    role: str | None = None
    event_count: int = 0


class AuditLogFilterOptionsResponse(BaseModel):
    """Distinct filter values to populate the FE dropdowns.

    Only values that actually appear in the caller's scope are returned,
    so the FE can render a tight, meaningful list rather than the full
    `AuditAction` enum.
    """

    users: list[AuditLogActorSummary] = Field(default_factory=list)
    actions: list[AuditAction] = Field(default_factory=list)
    entity_types: list[str] = Field(default_factory=list)
    date_min: datetime | None = None
    date_max: datetime | None = None


# --------------------------------------------------------------------------
# Anomaly detection (powers the FE purple "Review Anomaly" banner)
# --------------------------------------------------------------------------
class AuditLogAnomaly(BaseModel):
    """One detected anomaly window.

    `audit_log_ids` references the underlying audit rows that fired the
    rule — the FE can deep-link straight to them. Capped at 100 ids to
    keep the response bounded; if more than 100 matched, the first 50
    and last 50 are kept and `metadata.truncated` is set to True.
    """

    id: str = Field(..., description="Deterministic id: sha1(rule + actor + window_start)")
    rule: str = Field(..., description="Machine name (e.g. OVERRIDE_BURST)")
    severity: str = Field(..., description="LOW | MEDIUM | HIGH")
    title: str
    description: str
    actor_user_id: UUID | None = None
    actor_display_name: str | None = None
    window_start: datetime
    window_end: datetime
    event_count: int
    audit_log_ids: list[UUID] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AuditLogAnomalyResponse(BaseModel):
    """Anomaly detection result envelope."""

    anomalies: list[AuditLogAnomaly] = Field(default_factory=list)
    generated_at: datetime
    window_hours: int


__all__ = [
    "AuditLogResponse",
    "AuditLogActorSummary",
    "AuditLogFilterOptionsResponse",
    "AuditLogAnomaly",
    "AuditLogAnomalyResponse",
]
