"""Unit tests for notifications schemas — `NotificationResponse` shape."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from src.modules.notifications.schemas import (
    NotificationListResponse,
    NotificationResponse,
)
from src.shared.domain.enums import NotificationStatus, NotificationType


def _valid_kwargs() -> dict:
    return {
        "id": str(uuid.uuid4()),
        "agency_id": str(uuid.uuid4()),
        "recipient_user_id": str(uuid.uuid4()),
        "type": NotificationType.GENERIC.value,
        "title": "Hello",
        "body": "World",
        "status": NotificationStatus.SENT.value,
        "metadata": {"foo": "bar"},
        "created_at": "2026-06-27T10:00:00Z",
        "read_at": None,
        "expires_at": None,
    }


class TestNotificationResponse:
    def test_minimal_ok(self) -> None:
        n = NotificationResponse.model_validate(_valid_kwargs())
        assert n.type == NotificationType.GENERIC
        assert n.status == NotificationStatus.SENT
        assert n.metadata == {"foo": "bar"}

    def test_default_metadata_is_empty_dict(self) -> None:
        kw = _valid_kwargs()
        # Pydantic v2 default_factory=dict should handle missing key.
        kw.pop("metadata", None)
        n = NotificationResponse.model_validate(kw)
        assert n.metadata == {}

    def test_title_and_body_required(self) -> None:
        kw = _valid_kwargs()
        kw.pop("title")
        with pytest.raises(ValidationError):
            NotificationResponse.model_validate(kw)
        kw = _valid_kwargs()
        kw.pop("body")
        with pytest.raises(ValidationError):
            NotificationResponse.model_validate(kw)

    def test_accepts_orm_with_metadata_(self) -> None:
        """The ORM attribute is `metadata_`; the schema should map it."""

        class FakeORM:
            pass

        orm = FakeORM()
        orm.id = uuid.uuid4()
        orm.agency_id = uuid.uuid4()
        orm.recipient_user_id = uuid.uuid4()
        orm.type = NotificationType.SERVICE_VERIFIED
        orm.title = "Verified"
        orm.body = "All good"
        orm.status = NotificationStatus.SENT
        orm.metadata_ = {"k": "v"}
        orm.created_at = datetime.now(UTC)
        orm.read_at = None
        orm.expires_at = None

        n = NotificationResponse.model_validate(orm)
        assert n.metadata == {"k": "v"}


class TestNotificationListResponse:
    def test_basic_envelope(self) -> None:
        kw = _valid_kwargs()
        resp = NotificationListResponse(
            data=[NotificationResponse.model_validate(kw)],
            next_cursor="abc",
            unread_count=3,
        )
        assert resp.unread_count == 3
        assert resp.next_cursor == "abc"
        assert len(resp.data) == 1

    def test_next_cursor_optional(self) -> None:
        resp = NotificationListResponse(
            data=[],
            next_cursor=None,
            unread_count=0,
        )
        assert resp.next_cursor is None
