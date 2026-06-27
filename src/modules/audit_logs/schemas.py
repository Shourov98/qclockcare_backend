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


__all__ = ["AuditLogResponse"]
