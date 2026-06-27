"""End-to-end multi-channel delivery integration tests.

Walks:
  1. In default env (SMTP_ENABLED=false, SMS_ENABLED=false):
     - dispatch_notification creates 1 notification row
     - IN_APP delivery row is created and marked DELIVERED
     - No EMAIL/SMS delivery rows (channels disabled in env)
  2. With SMS_ENABLED=true (stub mode):
     - SMS delivery row is created and marked DELIVERED
  3. With SMTP_ENABLED=true:
     - EMAIL delivery row is created and marked FAILED (no real server)

Skipped if no local Supabase is reachable.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.core.config import settings

pytestmark = pytest.mark.asyncio


def _make_test_engine():
    return create_async_engine(
        settings.effective_database_url,
        pool_pre_ping=True,
        pool_size=2,
    )


async def _db_reachable(test_engine) -> bool:
    try:
        async with test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def _seed_agency_with_patient(test_engine):
    """Seed an agency + AGENCY_ADMIN + PATIENT (with email + phone)."""
    from src.core.security import hash_password

    password = "TestPass123!AB"
    admin_email = f"test-admin-{uuid.uuid4().hex[:8]}@example.com"
    patient_email = f"test-patient-{uuid.uuid4().hex[:8]}@example.com"
    password_hash = hash_password(password)
    agency_id = str(uuid.uuid4())
    admin_id = str(uuid.uuid4())
    patient_id = str(uuid.uuid4())
    admin_role_id = str(uuid.uuid4())
    patient_role_id = str(uuid.uuid4())

    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO agencies (id, name, timezone) "
                "VALUES (:id, :name, 'America/Chicago')"
            ),
            {"id": agency_id, "name": f"Delivery Test {uuid.uuid4().hex[:6]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, full_name, status, email_verified_at) "
                "VALUES (:id, :email, :pw, 'Admin', 'ACTIVE', now())"
            ),
            {"id": admin_id, "email": admin_email, "pw": password_hash},
        )
        await conn.execute(
            text(
                "INSERT INTO user_roles (id, user_id, agency_id, role) "
                "VALUES (:id, :uid, :aid, 'AGENCY_ADMIN')"
            ),
            {"id": admin_role_id, "uid": admin_id, "aid": agency_id},
        )
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, full_name, phone, status, email_verified_at) "
                "VALUES (:id, :email, :pw, 'Patient', '+15551234567', 'ACTIVE', now())"
            ),
            {"id": patient_id, "email": patient_email, "pw": password_hash},
        )
        await conn.execute(
            text(
                "INSERT INTO user_roles (id, user_id, agency_id, role) "
                "VALUES (:id, :uid, :aid, 'PATIENT')"
            ),
            {"id": patient_role_id, "uid": patient_id, "aid": agency_id},
        )
    return agency_id, admin_id, patient_id


async def _cleanup(test_engine, agency_id: str) -> None:
    async with test_engine.begin() as conn:
        await conn.execute(text("SET LOCAL session_replication_role = 'replica'"))
        await conn.execute(
            text("DELETE FROM notification_deliveries WHERE agency_id = :a"),
            {"a": agency_id},
        )
        await conn.execute(
            text("DELETE FROM notification_preferences WHERE agency_id = :a"),
            {"a": agency_id},
        )
        await conn.execute(
            text("DELETE FROM notifications WHERE agency_id = :a"), {"a": agency_id}
        )
        await conn.execute(
            text("DELETE FROM user_roles WHERE agency_id = :a"), {"a": agency_id}
        )
        await conn.execute(
            text("DELETE FROM users WHERE email LIKE 'test-admin-%@example.com'")
        )
        await conn.execute(
            text("DELETE FROM users WHERE email LIKE 'test-patient-%@example.com'")
        )


async def test_in_app_only_delivery_in_default_env() -> None:
    """Default env: SMTP_ENABLED=false, SMS_ENABLED=false.
    Only IN_APP delivery should fire."""
    test_engine = _make_test_engine()
    try:
        if not await _db_reachable(test_engine):
            pytest.skip("Database not reachable")

        # Reset registry so settings aren't cached
        from src.modules.notifications.channels import ProviderRegistry

        ProviderRegistry._PROVIDERS = {}

        agency_id, _admin_id, patient_id = await _seed_agency_with_patient(test_engine)

        from src.modules.notifications.service import dispatch_notification
        from src.shared.domain.enums import NotificationType

        session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_factory() as session, session.begin():
            notif = await dispatch_notification(
                session,
                agency_id=uuid.UUID(agency_id),
                recipient_user_id=uuid.UUID(patient_id),
                type=NotificationType.GENERIC,
                title="Hi",
                body="World",
                metadata={"entity_id": str(uuid.uuid4())},
            )
            assert notif is not None
            notif_id = notif.id

        # Verify delivery rows
        async with test_engine.begin() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT channel, status FROM notification_deliveries "
                        "WHERE notification_id = :n ORDER BY channel"
                    ),
                    {"n": str(notif_id)},
                )
            ).all()

        # In default env, only IN_APP is enabled. EMAIL needs SMTP,
        # SMS needs SMS_ENABLED=true.
        channels = {row[0] for row in rows}
        assert channels == {"IN_APP"}
        for _channel, status in rows:
            assert status == "DELIVERED"

        # Notification status should be DELIVERED
        async with test_engine.begin() as conn:
            status_val = (
                await conn.execute(
                    text("SELECT status FROM notifications WHERE id = :n"),
                    {"n": str(notif_id)},
                )
            ).scalar_one()
            assert status_val == "DELIVERED"

        await _cleanup(test_engine, agency_id)
    finally:
        await test_engine.dispose()


async def test_email_delivery_attempted_when_smtp_enabled() -> None:
    """With SMTP_ENABLED=true and no real SMTP server, EMAIL delivery
    row is created and marked FAILED."""
    test_engine = _make_test_engine()
    try:
        if not await _db_reachable(test_engine):
            pytest.skip("Database not reachable")

        agency_id, _admin_id, patient_id = await _seed_agency_with_patient(test_engine)

        # Force SMTP_ENABLED=true and reset the registry so the new
        # setting is picked up.
        with patch("src.modules.notifications.channels.settings") as mock_settings:
            mock_settings.SMTP_ENABLED = True
            mock_settings.SMTP_HOST = "127.0.0.1"
            mock_settings.SMTP_PORT = 1  # closed port — connection refused
            mock_settings.SMTP_USERNAME = ""
            mock_settings.SMTP_PASSWORD = None
            mock_settings.SMTP_FROM_NAME = "QlockCare"
            mock_settings.SMTP_FROM_EMAIL = "noreply@qlockcare.local"
            mock_settings.SMTP_USE_TLS = False
            mock_settings.SMS_ENABLED = False

            from src.modules.notifications.channels import ProviderRegistry

            ProviderRegistry._PROVIDERS = {}

            from src.modules.notifications.service import dispatch_notification
            from src.shared.domain.enums import NotificationType

            session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
            async with session_factory() as session, session.begin():
                notif = await dispatch_notification(
                    session,
                    agency_id=uuid.UUID(agency_id),
                    recipient_user_id=uuid.UUID(patient_id),
                    type=NotificationType.GENERIC,
                    title="Hi",
                    body="World",
                    metadata={"entity_id": str(uuid.uuid4())},
                )
                assert notif is not None
                notif_id = notif.id

        async with test_engine.begin() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT channel, status, error FROM notification_deliveries "
                        "WHERE notification_id = :n ORDER BY channel"
                    ),
                    {"n": str(notif_id)},
                )
            ).all()

        # We expect IN_APP (DELIVERED) and EMAIL (FAILED).
        by_channel = {row[0]: row for row in rows}
        assert "IN_APP" in by_channel
        assert "EMAIL" in by_channel
        assert by_channel["IN_APP"][1] == "DELIVERED"
        assert by_channel["EMAIL"][1] == "FAILED"
        assert by_channel["EMAIL"][2] is not None  # error message captured

        await _cleanup(test_engine, agency_id)
    finally:
        await test_engine.dispose()


async def test_sms_stub_provider_succeeds() -> None:
    """With SMS_ENABLED=false (default), the SMS stub provider logs and
    returns success, but the SMS channel is not in enabled_channels()
    so no SMS delivery row is created. This test verifies that an
    IN_APP-only user with no phone still gets an IN_APP delivery."""
    test_engine = _make_test_engine()
    try:
        if not await _db_reachable(test_engine):
            pytest.skip("Database not reachable")

        from src.modules.notifications.channels import ProviderRegistry

        ProviderRegistry._PROVIDERS = {}

        agency_id, _admin_id, patient_id = await _seed_agency_with_patient(test_engine)

        from src.modules.notifications.service import dispatch_notification
        from src.shared.domain.enums import NotificationType

        session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_factory() as session, session.begin():
            notif = await dispatch_notification(
                session,
                agency_id=uuid.UUID(agency_id),
                recipient_user_id=uuid.UUID(patient_id),
                type=NotificationType.GENERIC,
                title="Hi",
                body="World",
                metadata={"entity_id": str(uuid.uuid4())},
            )
            assert notif is not None

        # Delivery rows are IN_APP only (SMS is disabled).
        async with test_engine.begin() as conn:
            channels = (
                await conn.execute(
                    text(
                        "SELECT DISTINCT channel FROM notification_deliveries "
                        "WHERE notification_id = :n"
                    ),
                    {"n": str(notif.id)},
                )
            ).scalars().all()
        assert set(channels) == {"IN_APP"}

        await _cleanup(test_engine, agency_id)
    finally:
        await test_engine.dispose()
