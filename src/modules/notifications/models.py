"""Notifications ORM models.

Three tables:

- `notifications` — one row per notification dispatched to a recipient.
  Carries the typed payload (`type` enum) + a free-form `metadata`
  jsonb for entity references and client display data.
- `notification_preferences` — per (user_id, type, channel) opt-in /
  opt-out. Default is opted-in for all combos; rows are lazy-seeded
  at first read by `preferences.get_or_create_prefs`.
- `notification_deliveries` — per (notification_id, channel) attempt
  log. The multi-channel dispatcher writes one row per channel it tries.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.domain.base_entity import Base, IdMixin
from src.shared.domain.enum_mapping import pg_name
from src.shared.domain.enums import (
    NotificationChannel,
    NotificationStatus,
    NotificationType,
)


class Notification(IdMixin, Base):
    """In-app notification row.

    `metadata` is a jsonb blob carrying entity references the client
    needs to deep-link into the relevant screen (e.g. `{"visit_id":
    "...", "appointment_id": "..."}`). It is intentionally a free-form
    dict per type — strict typing lives in the dispatcher's helper.
    """

    __tablename__ = "notifications"

    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id", ondelete="CASCADE"),
        nullable=False,
    )
    recipient_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    type: Mapped[NotificationType] = mapped_column(
        Enum(NotificationType, name=pg_name(NotificationType)),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[NotificationStatus] = mapped_column(
        Enum(NotificationStatus, name=pg_name(NotificationStatus)),
        nullable=False,
        default=NotificationStatus.SENT,  # in-app delivery is instantaneous
        server_default=NotificationStatus.SENT.value,
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index(
            "idx_notifications_recipient_unread",
            "recipient_user_id",
            text("created_at DESC"),
            postgresql_where=text("read_at IS NULL AND status <> 'FAILED'"),
        ),
        Index(
            "idx_notifications_recipient",
            "recipient_user_id",
            text("created_at DESC"),
        ),
        Index(
            "idx_notifications_agency_type",
            "agency_id",
            "type",
            text("created_at DESC"),
        ),
        CheckConstraint(
            "title <> '' AND length(trim(title)) > 0",
            name="ck_notifications_title_non_empty",
        ),
        CheckConstraint(
            "body <> '' AND length(trim(body)) > 0",
            name="ck_notifications_body_non_empty",
        ),
        CheckConstraint(
            "(read_at IS NULL) OR (status = 'READ')",
            name="ck_notifications_read_at_implies_status_read",
        ),
        CheckConstraint(
            "(status <> 'READ') OR (read_at IS NOT NULL)",
            name="ck_notifications_status_read_implies_read_at",
        ),
    )


class NotificationPreference(Base):
    """Per-(user, type, channel) opt-in/opt-out.

    Composite primary key (user_id, type, channel) — the dispatcher
    always queries by (user_id, type, channel) and the hot-path check
    is one row.
    """

    __tablename__ = "notification_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id", ondelete="CASCADE"),
        nullable=False,
    )
    type: Mapped[NotificationType] = mapped_column(
        Enum(NotificationType, name=pg_name(NotificationType)),
        nullable=False,
    )
    channel: Mapped[NotificationChannel] = mapped_column(
        Enum(NotificationChannel, name=pg_name(NotificationChannel)),
        nullable=False,
    )
    opted_in: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    __table_args__ = (
        PrimaryKeyConstraint(
            "user_id", "type", "channel", name="pk_notification_preferences"
        ),
        Index("idx_notification_prefs_user", "user_id"),
        Index("idx_notification_prefs_agency", "agency_id", "type"),
    )


class NotificationDelivery(IdMixin, Base):
    """One row per (notification, channel) delivery attempt.

    Written by the multi-channel dispatcher as it fans a notification
    out to each enabled channel. Updated in place when the provider
    acks (status transitions PENDING → SENT/FAILED/BOUNCED/DELIVERED).
    """

    __tablename__ = "notification_deliveries"

    notification_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("notifications.id", ondelete="CASCADE"),
        nullable=False,
    )
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel: Mapped[NotificationChannel] = mapped_column(
        Enum(NotificationChannel, name=pg_name(NotificationChannel)),
        nullable=False,
    )
    status: Mapped[NotificationStatus] = mapped_column(
        Enum(NotificationStatus, name=pg_name(NotificationStatus)),
        nullable=False,
        default=NotificationStatus.PENDING,
        server_default=NotificationStatus.PENDING.value,
    )
    provider_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "notification_id",
            "channel",
            name="uq_notification_deliveries_notification_channel",
        ),
        Index("idx_notification_deliveries_notification", "notification_id"),
        Index(
            "idx_notification_deliveries_agency_status",
            "agency_id",
            "status",
            text("created_at DESC"),
        ),
    )


__all__ = [
    "Notification",
    "NotificationDelivery",
    "NotificationPreference",
]
