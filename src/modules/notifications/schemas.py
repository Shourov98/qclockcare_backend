"""Notifications schemas — request/response DTOs for `/notifications`.

The endpoint surface is intentionally narrow:
- GET    /notifications               — list the caller's notifications (cursor paginated)
- GET    /notifications/{id}          — single notification
- PATCH  /notifications/{id}/read     — mark as read
- POST   /notifications/read-all      — mark all as read

Writes (insert) are NOT exposed via HTTP — they happen through the
dispatcher (`notifications_service.dispatch_notification`) called by
other modules (visits, appointments, etc).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.shared.domain.enums import NotificationStatus, NotificationType


class NotificationResponse(BaseModel):
    """Single notification — shape returned by all read endpoints."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    agency_id: UUID
    recipient_user_id: UUID
    type: NotificationType
    title: str
    body: str
    status: NotificationStatus
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    read_at: datetime | None
    expires_at: datetime | None

    @classmethod
    def model_validate(cls, obj):  # type: ignore[override]
        # The ORM attribute is `metadata_` (Python keyword workaround); the
        # JSON column name is `metadata`. Map them here.
        if hasattr(obj, "metadata_"):
            data = {
                "id": obj.id,
                "agency_id": obj.agency_id,
                "recipient_user_id": obj.recipient_user_id,
                "type": obj.type,
                "title": obj.title,
                "body": obj.body,
                "status": obj.status,
                "metadata": obj.metadata_,
                "created_at": obj.created_at,
                "read_at": obj.read_at,
                "expires_at": obj.expires_at,
            }
            return super().model_validate(data)
        return super().model_validate(obj)


class NotificationListResponse(BaseModel):
    """Cursor-paginated list envelope.

    `next_cursor` is None on the last page; clients pass it back as a
    query param to fetch the next page. `unread_count` is a convenience
    field so the UI can render the bell badge without a second call.
    """

    model_config = ConfigDict(from_attributes=True)

    data: list[NotificationResponse]
    next_cursor: str | None
    unread_count: int


__all__ = [
    "NotificationListResponse",
    "NotificationResponse",
]
