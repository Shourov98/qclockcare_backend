"""Unit tests for notification preferences + broadcast + badge schemas.

Covers:
  - NotificationBadgeResponse (cheap envelope)
  - NotificationPreferenceResponse + NotificationPreferenceUpdateRequest
  - BroadcastRequest + BroadcastResponse
  - DeliveryResponse
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from src.modules.notifications.schemas import (
    BroadcastRequest,
    BroadcastResponse,
    DeliveryResponse,
    NotificationBadgeResponse,
    NotificationPreferenceResponse,
    NotificationPreferenceUpdateRequest,
)
from src.shared.domain.enums import (
    NotificationChannel,
    NotificationStatus,
    NotificationType,
)


class TestNotificationBadgeResponse:
    def test_basic(self) -> None:
        r = NotificationBadgeResponse(unread_count=5)
        assert r.unread_count == 5

    def test_zero(self) -> None:
        r = NotificationBadgeResponse(unread_count=0)
        assert r.unread_count == 0


class TestNotificationPreferenceResponse:
    def test_basic(self) -> None:
        now = datetime.now(UTC)
        r = NotificationPreferenceResponse(
            user_id=uuid.uuid4(),
            type=NotificationType.VISIT_CHECKED_OUT,
            channel=NotificationChannel.IN_APP,
            opted_in=False,
            updated_at=now,
        )
        assert r.opted_in is False
        assert r.type == NotificationType.VISIT_CHECKED_OUT
        assert r.channel == NotificationChannel.IN_APP
        assert r.updated_at == now


class TestNotificationPreferenceUpdateRequest:
    def test_opted_in_true(self) -> None:
        r = NotificationPreferenceUpdateRequest(opted_in=True)
        assert r.opted_in is True

    def test_opted_in_false(self) -> None:
        r = NotificationPreferenceUpdateRequest(opted_in=False)
        assert r.opted_in is False

    def test_opted_in_required(self) -> None:
        with pytest.raises(ValidationError):
            NotificationPreferenceUpdateRequest()  # type: ignore[call-arg]


class TestBroadcastRequest:
    def test_minimal(self) -> None:
        r = BroadcastRequest(
            type=NotificationType.GENERIC,
            title="Heads up",
            body="Maintenance Sunday 2am.",
        )
        assert r.type == NotificationType.GENERIC
        assert r.channel_filter == [NotificationChannel.IN_APP]
        assert r.metadata == {}

    def test_with_metadata_and_channels(self) -> None:
        r = BroadcastRequest(
            type=NotificationType.GENERIC,
            title="Hi",
            body="World",
            metadata={"campaign_id": "x"},
            channel_filter=[NotificationChannel.IN_APP, NotificationChannel.EMAIL],
        )
        assert r.metadata["campaign_id"] == "x"
        assert NotificationChannel.EMAIL in r.channel_filter

    def test_empty_title_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BroadcastRequest(
                type=NotificationType.GENERIC,
                title="",
                body="World",
            )

    def test_empty_body_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BroadcastRequest(
                type=NotificationType.GENERIC,
                title="Hi",
                body="",
            )

    def test_title_max_length(self) -> None:
        with pytest.raises(ValidationError):
            BroadcastRequest(
                type=NotificationType.GENERIC,
                title="x" * 501,
                body="World",
            )


class TestBroadcastResponse:
    def test_basic(self) -> None:
        r = BroadcastResponse(dispatched=10, skipped_opted_out=2, failed=1)
        assert r.dispatched == 10
        assert r.skipped_opted_out == 2
        assert r.failed == 1

    def test_zero_dispatched(self) -> None:
        r = BroadcastResponse(dispatched=0, skipped_opted_out=0, failed=0)
        assert r.dispatched == 0


class TestDeliveryResponse:
    def test_basic(self) -> None:
        now = datetime.now(UTC)
        r = DeliveryResponse(
            id=uuid.uuid4(),
            notification_id=uuid.uuid4(),
            channel=NotificationChannel.EMAIL,
            status=NotificationStatus.SENT,
            provider_message_id=None,
            error=None,
            created_at=now,
            delivered_at=None,
        )
        assert r.channel == NotificationChannel.EMAIL
        assert r.status == NotificationStatus.SENT
        assert r.provider_message_id is None
        assert r.delivered_at is None

    def test_failed_with_error(self) -> None:
        now = datetime.now(UTC)
        r = DeliveryResponse(
            id=uuid.uuid4(),
            notification_id=uuid.uuid4(),
            channel=NotificationChannel.EMAIL,
            status=NotificationStatus.FAILED,
            provider_message_id=None,
            error="SMTP connection refused",
            created_at=now,
            delivered_at=None,
        )
        assert r.status == NotificationStatus.FAILED
        assert r.error == "SMTP connection refused"
