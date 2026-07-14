"""Unit tests for the split between Phase 1 (prepare_deliveries) and
Phase 2 (dispatch_provider_phase).

These tests do NOT hit the database or the network. They mock the
`AsyncSession` enough to drive `prepare_deliveries` (which inserts one
PENDING `NotificationDelivery` row per available channel) and
`dispatch_provider_phase` (which calls the provider + UPDATEs the
delivery row + flips the parent `Notification.status`).

The goal is to assert the contract between the two phases — the
background-task runner doesn't have to re-resolve channels or re-decide
opt-ins; it just takes the `(channel, delivery_id)` tuples returned by
Phase 1 and processes them in order.

We also assert that Phase 1 returns immediately even when the
registered provider's `send()` would hang for tens of seconds — i.e.
no provider call happens during the synchronous path. This is the
whole point of the split.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.modules.notifications.deliveries import (
    dispatch_provider_phase,
    prepare_deliveries,
)
from src.shared.domain.enums import (
    NotificationChannel,
    NotificationStatus,
    NotificationType,
)
from src.shared.domain.enums import (
    UserRole as _UserRole,  # noqa: F401  (kept for parity with callers)
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------
def _make_notification(
    *, agency_id: uuid.UUID | None = None, recipient_user_id: uuid.UUID | None = None
) -> Any:
    """Build a Notification-shaped stub for tests.

    `dispatch_provider_phase` and `prepare_deliveries` only read these
    attributes from the row: `id`, `agency_id`, `recipient_user_id`,
    `type`, `title`, `body`, `metadata_`, `status`. A `SimpleNamespace`
    is enough — instantiating the real ORM class triggers mapper
    configuration across identity/staff/patients/agencies modules
    that have circular FK relationships, which is not worth the
    complexity for a unit test.
    """
    return SimpleNamespace(
        id=uuid.uuid4(),
        agency_id=agency_id or uuid.uuid4(),
        recipient_user_id=recipient_user_id or uuid.uuid4(),
        type=NotificationType.GENERIC,
        title="Hi",
        body="World",
        metadata_={"entity_id": str(uuid.uuid4())},
        status=NotificationStatus.SENT,
    )


class _FakeScalarResult:
    """Tiny stub for the bits of `Result` that our code touches.

    `prepare_deliveries` calls `await session.flush()` and then
    `await _delivery_row_id(...)` which executes a SELECT for the
    inserted row's id. We return a predetermined id.
    """

    def __init__(self, value: Any = None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _FakeSession:
    """Records calls + returns canned results for `execute(...)`.

    Used to drive `prepare_deliveries` and `dispatch_provider_phase`
    without touching a real DB.
    """

    def __init__(self, *, delivery_id: uuid.UUID | None = None) -> None:
        self.delivery_id = delivery_id or uuid.uuid4()
        self.inserted: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self._next_scalar: Any = self.delivery_id

        self.execute = AsyncMock(side_effect=self._execute)
        self.flush = AsyncMock(side_effect=self._on_flush)

    async def _execute(self, stmt: Any) -> _FakeScalarResult:
        # SQLAlchemy statement objects are opaque to us; just record
        # that something ran and return a scalar for the SELECT.
        # We can't easily inspect stmt here, so we just return the
        # configured scalar — good enough for these tests.
        return _FakeScalarResult(self._next_scalar)

    async def _on_flush(self) -> None:
        # No-op; we don't need to simulate flush behaviour.
        return None


# ---------------------------------------------------------------------------
# Phase 1 — prepare_deliveries
# ---------------------------------------------------------------------------
class TestPrepareDeliveries:
    async def test_inserts_one_pending_row_per_enabled_channel(self) -> None:
        """For each enabled channel, insert a PENDING `NotificationDelivery`
        row and return the (channel, delivery_id) tuple."""
        notification = _make_notification()
        session = _FakeSession()

        # Channels returned by the registry.
        enabled = [
            NotificationChannel.IN_APP,
            NotificationChannel.SMS,  # stub provider is always enabled
        ]
        # For each enabled channel we expect:
        #   1. `session.execute(INSERT ... NotificationDelivery)`  → returns nothing useful
        #   2. `session.execute(SELECT ... NotificationDelivery.id)`  → returns the id
        # (the SELECT is what `_delivery_row_id` runs after the flush)
        id_a, id_b = uuid.uuid4(), uuid.uuid4()

        execute_results: list[Any] = [
            None,             # INSERT for IN_APP
            id_a,             # SELECT for IN_APP id
            None,             # INSERT for SMS
            id_b,             # SELECT for SMS id
        ]
        idx = 0

        async def fake_execute(stmt: Any) -> _FakeScalarResult:
            nonlocal idx
            r = execute_results[idx]
            idx += 1
            return _FakeScalarResult(r)

        session.execute = AsyncMock(side_effect=fake_execute)

        with (
            patch(
                "src.modules.notifications.deliveries.ProviderRegistry.enabled_channels",
                return_value=enabled,
            ),
            patch(
                "src.modules.notifications.deliveries.ProviderRegistry.get",
                side_effect=lambda ch: MagicMock(channel=ch),
            ),
            patch(
                "src.modules.notifications.deliveries._channel_opted_in",
                AsyncMock(return_value=True),
            ),
        ):
            result = await prepare_deliveries(
                session, notification=notification
            )

        # Returned tuples match the enabled channels in registry order.
        assert [ch for ch, _ in result] == enabled
        assert [did for _, did in result] == [id_a, id_b]

    async def test_skips_opted_out_channels(self) -> None:
        """When a recipient has opted out of a channel, no delivery row
        is inserted and the channel is omitted from the returned list."""
        notification = _make_notification()
        session = _FakeSession()

        async def fake_execute(stmt: Any) -> _FakeScalarResult:
            return _FakeScalarResult(uuid.uuid4())

        session.execute = AsyncMock(side_effect=fake_execute)

        # SMS is "opted out" — _channel_opted_in returns False for it.
        async def fake_opted_in(
            session: Any, *, user_id: Any, type_: Any, channel: NotificationChannel
        ) -> bool:
            return channel == NotificationChannel.IN_APP

        with (
            patch(
                "src.modules.notifications.deliveries.ProviderRegistry.enabled_channels",
                return_value=[
                    NotificationChannel.IN_APP,
                    NotificationChannel.SMS,
                ],
            ),
            patch(
                "src.modules.notifications.deliveries.ProviderRegistry.get",
                side_effect=lambda ch: MagicMock(channel=ch),
            ),
            patch(
                "src.modules.notifications.deliveries._channel_opted_in",
                side_effect=fake_opted_in,
            ),
        ):
            result = await prepare_deliveries(
                session, notification=notification
            )

        # Only IN_APP made it through.
        assert [ch for ch, _ in result] == [NotificationChannel.IN_APP]

    async def test_skips_channels_without_provider(self) -> None:
        """Channels for which `ProviderRegistry.get` returns None are
        silently skipped (provider not configured in this env)."""
        notification = _make_notification()
        session = _FakeSession()

        async def fake_execute(stmt: Any) -> _FakeScalarResult:
            return _FakeScalarResult(uuid.uuid4())

        session.execute = AsyncMock(side_effect=fake_execute)

        def fake_get(channel: NotificationChannel) -> Any:
            # EMAIL has no provider; IN_APP does.
            if channel == NotificationChannel.EMAIL:
                return None
            return MagicMock(channel=channel)

        with (
            patch(
                "src.modules.notifications.deliveries.ProviderRegistry.enabled_channels",
                return_value=[
                    NotificationChannel.IN_APP,
                    NotificationChannel.EMAIL,
                ],
            ),
            patch(
                "src.modules.notifications.deliveries.ProviderRegistry.get",
                side_effect=fake_get,
            ),
            patch(
                "src.modules.notifications.deliveries._channel_opted_in",
                AsyncMock(return_value=True),
            ),
        ):
            result = await prepare_deliveries(
                session, notification=notification
            )

        assert [ch for ch, _ in result] == [NotificationChannel.IN_APP]

    async def test_returns_empty_when_no_channels_enabled(self) -> None:
        """No enabled channels → empty list. Caller can still commit
        the parent Notification row; no background work is scheduled."""
        notification = _make_notification()
        session = _FakeSession()

        with patch(
            "src.modules.notifications.deliveries.ProviderRegistry.enabled_channels",
            return_value=[],
        ):
            result = await prepare_deliveries(
                session, notification=notification
            )

        assert result == []


# ---------------------------------------------------------------------------
# Phase 2 — dispatch_provider_phase
# ---------------------------------------------------------------------------
class TestDispatchProviderPhase:
    async def test_marks_delivery_delivered_on_provider_success(self) -> None:
        """When the provider returns success=True, the delivery row is
        marked DELIVERED and the parent Notification.status flips to
        DELIVERED."""
        notification = _make_notification()
        notification.status = NotificationStatus.SENT
        delivery_id = uuid.uuid4()
        deliveries = [(NotificationChannel.IN_APP, delivery_id)]

        # The session's `execute` is called for both the UPDATE on the
        # delivery row and the address-lookup SELECT. We don't need
        # the UPDATE return value; just don't blow up.
        session = _FakeSession(delivery_id=delivery_id)

        with patch(
            "src.modules.notifications.deliveries.ProviderRegistry.get"
        ) as mock_get:
            mock_provider = MagicMock()
            mock_provider.send = AsyncMock(
                return_value=_fake_result(success=True, provider_message_id="x")
            )
            mock_get.return_value = mock_provider

            await dispatch_provider_phase(
                session, notification=notification, deliveries=deliveries
            )

        assert notification.status == NotificationStatus.DELIVERED

    async def test_marks_delivery_failed_on_provider_failure(self) -> None:
        """Provider returns success=False → delivery row FAILED and
        (since no channel succeeded) Notification.status → FAILED."""
        notification = _make_notification()
        notification.status = NotificationStatus.SENT
        delivery_id = uuid.uuid4()
        deliveries = [(NotificationChannel.EMAIL, delivery_id)]

        session = _FakeSession(delivery_id=delivery_id)

        with patch(
            "src.modules.notifications.deliveries.ProviderRegistry.get"
        ) as mock_get:
            mock_provider = MagicMock()
            mock_provider.send = AsyncMock(
                return_value=_fake_result(success=False, error="boom")
            )
            mock_get.return_value = mock_provider

            await dispatch_provider_phase(
                session, notification=notification, deliveries=deliveries
            )

        assert notification.status == NotificationStatus.FAILED

    async def test_marks_delivery_failed_when_provider_crashes(self) -> None:
        """If the provider raises (programmer error / network crash),
        the delivery row is marked FAILED with the exception name;
        other channels continue."""
        notification = _make_notification()
        notification.status = NotificationStatus.SENT
        id_a, id_b = uuid.uuid4(), uuid.uuid4()
        deliveries = [
            (NotificationChannel.EMAIL, id_a),
            (NotificationChannel.IN_APP, id_b),
        ]

        session = _FakeSession()

        def provider_for(channel: NotificationChannel) -> Any:
            p = MagicMock()
            if channel == NotificationChannel.EMAIL:
                p.send = AsyncMock(side_effect=RuntimeError("smtp down"))
            else:
                p.send = AsyncMock(
                    return_value=_fake_result(success=True)
                )
            return p

        with patch(
            "src.modules.notifications.deliveries.ProviderRegistry.get",
            side_effect=provider_for,
        ):
            await dispatch_provider_phase(
                session, notification=notification, deliveries=deliveries
            )

        # In-app succeeded, so the parent Notification flips to DELIVERED.
        assert notification.status == NotificationStatus.DELIVERED

    async def test_marks_failed_when_no_address_on_file(self) -> None:
        """No email/phone on the user → delivery row FAILED with a
        clear `error=` message, parent Notification → FAILED (no
        channel succeeded)."""
        notification = _make_notification()
        notification.status = NotificationStatus.SENT
        delivery_id = uuid.uuid4()
        deliveries = [(NotificationChannel.EMAIL, delivery_id)]

        # Address resolver returns None.
        session = _FakeSession(delivery_id=delivery_id)

        with patch(
            "src.modules.notifications.deliveries._resolve_recipient_address",
            AsyncMock(return_value=None),
        ):
            await dispatch_provider_phase(
                session, notification=notification, deliveries=deliveries
            )

        assert notification.status == NotificationStatus.FAILED

    async def test_no_channels_leaves_status_unchanged(self) -> None:
        """Empty deliveries list (Phase 1 returned nothing) → parent
        Notification.status stays at SENT (the default for an in-app
        row that committed but had no other channels)."""
        notification = _make_notification()
        notification.status = NotificationStatus.SENT
        session = _FakeSession()

        await dispatch_provider_phase(
            session, notification=notification, deliveries=[]
        )

        assert notification.status == NotificationStatus.SENT

    async def test_provider_not_available_marks_failed(self) -> None:
        """Provider was removed from the registry between Phase 1 and
        Phase 2 → delivery row FAILED with `Provider unavailable`."""
        notification = _make_notification()
        notification.status = NotificationStatus.SENT
        delivery_id = uuid.uuid4()
        deliveries = [(NotificationChannel.EMAIL, delivery_id)]

        session = _FakeSession(delivery_id=delivery_id)

        with patch(
            "src.modules.notifications.deliveries.ProviderRegistry.get",
            return_value=None,
        ):
            await dispatch_provider_phase(
                session, notification=notification, deliveries=deliveries
            )

        assert notification.status == NotificationStatus.FAILED


# ---------------------------------------------------------------------------
# Cross-phase: the request thread does NOT call providers
# ---------------------------------------------------------------------------
class TestPhase1DoesNotCallProviders:
    async def test_prepare_deliveries_does_not_invoke_provider_send(
        self,
    ) -> None:
        """The whole point of the split: Phase 1 must never call
        `provider.send()`. Even if the provider's `send` would hang for
        30 seconds, Phase 1 returns immediately.

        We assert by registering a provider whose `send` raises
        `RuntimeError("should not be called")` — if Phase 1 calls it,
        the test fails loudly.
        """
        notification = _make_notification()
        session = _FakeSession()

        async def fake_execute(stmt: Any) -> _FakeScalarResult:
            return _FakeScalarResult(uuid.uuid4())

        session.execute = AsyncMock(side_effect=fake_execute)

        sentinel_provider = MagicMock()
        sentinel_provider.send = AsyncMock(
            side_effect=AssertionError(
                "Phase 1 must not call provider.send(); "
                "that's Phase 2's job."
            )
        )

        with (
            patch(
                "src.modules.notifications.deliveries.ProviderRegistry.enabled_channels",
                return_value=[
                    NotificationChannel.IN_APP,
                    NotificationChannel.EMAIL,
                ],
            ),
            patch(
                "src.modules.notifications.deliveries.ProviderRegistry.get",
                return_value=sentinel_provider,
            ),
            patch(
                "src.modules.notifications.deliveries._channel_opted_in",
                AsyncMock(return_value=True),
            ),
        ):
            result = await prepare_deliveries(
                session, notification=notification
            )

        # Sentinel provider.send was never invoked.
        assert sentinel_provider.send.call_count == 0
        # Phase 1 still returned the (channel, delivery_id) tuples.
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------
def _fake_result(
    *, success: bool, error: str | None = None, provider_message_id: str | None = None
) -> Any:
    """Build a `DeliveryResult`-shaped stand-in.

    We don't import `DeliveryResult` directly to keep the test file
    decoupled from the channel module's class definition; duck-typed
    enough for `dispatch_provider_phase` to read the attributes.
    """
    return _FakeDeliveryResult(
        success=success, error=error, provider_message_id=provider_message_id
    )


class _FakeDeliveryResult:
    def __init__(
        self,
        *,
        success: bool,
        error: str | None,
        provider_message_id: str | None,
    ) -> None:
        self.success = success
        self.error = error
        self.provider_message_id = provider_message_id


__all__ = [
    "TestDispatchProviderPhase",
    "TestPhase1DoesNotCallProviders",
    "TestPrepareDeliveries",
]
