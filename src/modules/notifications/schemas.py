"""Notifications schemas — request/response DTOs for `/notifications`.

The endpoint surface covers:
- GET    /notifications               — list the caller's notifications (cursor paginated)
- GET    /notifications/badge         — cheap unread-count for bell icon
- GET    /notifications/preferences   — list caller's per-(type, channel) opt-in/opt-out
- PUT    /notifications/preferences/{type}/{channel} — toggle one pref
- POST   /notifications/broadcast     — AGENCY_ADMIN fan-out to all ACTIVE users in the agency
- GET    /notifications/{id}          — single notification
- PATCH  /notifications/{id}/read     — mark as read
- POST   /notifications/read-all      — mark all as read

Writes (insert) are NOT exposed via HTTP for per-recipient notifications —
they happen through the dispatcher (`notifications_service.dispatch_notification`)
called by other modules (visits, appointments, etc). The broadcast endpoint
is the one HTTP-driven writer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.modules.notifications.models import Notification
from src.shared.domain.enums import (
    NotificationChannel,
    NotificationStatus,
    NotificationType,
)


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
    def model_validate(
        cls, obj: Any, **kwargs: Any
    ) -> NotificationResponse:
        # The ORM attribute is `metadata_` (Python keyword workaround); the
        # JSON column name is `metadata`. Map them here. Both real
        # `Notification` ORM instances AND duck-typed fakes (used in
        # unit tests) need the explicit mapping because Pydantic's
        # `from_attributes=True` cannot follow the renamed attribute.
        if isinstance(obj, Notification) or hasattr(obj, "metadata_"):
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


class NotificationBadgeResponse(BaseModel):
    """Unread-count response for the bell icon.

    Cheap endpoint — just a SELECT COUNT(*). The full list endpoint
    also returns unread_count for backward compatibility, but the
    navbar should poll `/badge` instead to avoid pulling a page.
    """

    model_config = ConfigDict(from_attributes=True)

    unread_count: int


class NotificationPreferenceResponse(BaseModel):
    """One (user, type, channel) opt-in/opt-out row.

    `updated_at` is set on every write so the client can detect when
    the server last changed the row.
    """

    model_config = ConfigDict(from_attributes=True)

    user_id: UUID
    type: NotificationType
    channel: NotificationChannel
    opted_in: bool
    updated_at: datetime


class NotificationPreferenceUpdateRequest(BaseModel):
    """Body for PUT /notifications/preferences/{type}/{channel}.

    Only `opted_in` is mutable — the (user, type, channel) tuple is
    part of the URL and identifies the row.
    """

    opted_in: bool


class BroadcastRequest(BaseModel):
    """Body for POST /notifications/broadcast.

    AGENCY_ADMIN (or SUPER_ADMIN) sends a notice to every ACTIVE user
    in their agency. `channel_filter` lets ops pick which channels
    receive the broadcast (default = in-app only). `metadata` is a
    free-form dict carried on each generated notification row for
    client deep-linking.
    """

    type: NotificationType
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=4000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    channel_filter: list[NotificationChannel] = Field(
        default_factory=lambda: [NotificationChannel.IN_APP]
    )


class BroadcastResponse(BaseModel):
    """Result of a broadcast — counts only (no per-recipient rows)."""

    model_config = ConfigDict(from_attributes=True)

    dispatched: int
    skipped_opted_out: int
    failed: int


class DeliveryResponse(BaseModel):
    """One (notification, channel) delivery attempt log row."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    notification_id: UUID
    channel: NotificationChannel
    status: NotificationStatus
    provider_message_id: str | None
    error: str | None
    created_at: datetime
    delivered_at: datetime | None


__all__ = [
    "BroadcastRequest",
    "BroadcastResponse",
    "DeliveryResponse",
    "NotificationBadgeResponse",
    "NotificationListResponse",
    "NotificationPreferenceResponse",
    "NotificationPreferenceUpdateRequest",
    "NotificationResponse",
]
