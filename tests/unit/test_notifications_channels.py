"""Unit tests for the pluggable notification channel providers.

Covers:
  - DeliveryResult dataclass defaults
  - InAppProvider always succeeds
  - EmailProvider returns failure when neither Resend nor SMTP is enabled
  - EmailProvider routes to Resend when RESEND_ENABLED=true with a key
  - EmailProvider falls back to SMTP when Resend is disabled
  - EmailProvider prefers Resend over SMTP when both are enabled
  - EmailProvider handles Resend non-2xx responses
  - SMSProvider returns success in stub mode (SMS_ENABLED=false)
  - SMSProvider raises NotImplementedError when SMS_ENABLED=true and
    Twilio creds are missing
  - ProviderRegistry.enabled_channels() reflects env-gated activation
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.modules.notifications.channels import (
    DeliveryResult,
    EmailProvider,
    InAppProvider,
    NotificationProvider,
    ProviderRegistry,
    SMSProvider,
)
from src.shared.domain.enums import NotificationChannel


class TestDeliveryResult:
    def test_minimal(self) -> None:
        r = DeliveryResult(success=True)
        assert r.success is True
        assert r.provider_message_id is None
        assert r.error is None

    def test_with_error(self) -> None:
        r = DeliveryResult(success=False, error="boom")
        assert r.success is False
        assert r.error == "boom"

    def test_with_provider_id(self) -> None:
        r = DeliveryResult(success=True, provider_message_id="twilio-123")
        assert r.provider_message_id == "twilio-123"

    def test_frozen(self) -> None:
        r = DeliveryResult(success=True)
        with pytest.raises((AttributeError, Exception)):
            r.success = False  # type: ignore[misc]


class TestInAppProvider:
    async def test_always_succeeds(self) -> None:
        provider = InAppProvider()
        result = await provider.send(
            to="user-id", subject="Hi", body="There", metadata={"k": "v"}
        )
        assert result.success is True
        assert result.provider_message_id == "in-app:user-id"
        assert result.error is None

    def test_channel_is_in_app(self) -> None:
        assert InAppProvider.channel == NotificationChannel.IN_APP


class TestEmailProvider:
    async def test_disabled_returns_failure(self) -> None:
        with patch("src.modules.notifications.channels.settings") as mock_settings:
            mock_settings.SMTP_ENABLED = False
            mock_settings.RESEND_ENABLED = False
            mock_settings.RESEND_API_KEY = None
            mock_settings.SMTP_FROM_NAME = "QlockCare"
            mock_settings.SMTP_FROM_EMAIL = "noreply@qlockcare.local"
            provider = EmailProvider()
            result = await provider.send(
                to="x@example.com", subject="Hi", body="There"
            )
            assert result.success is False
            assert "Email disabled" in (result.error or "")

    async def test_resend_enabled_calls_api(self) -> None:
        """RESEND_ENABLED=true + key → Resend branch."""
        from pydantic import SecretStr

        fake_response = MagicMock()
        fake_response.status_code = 201
        fake_response.json.return_value = {"id": "abc123"}
        fake_response.text = '{"id": "abc123"}'

        fake_client = AsyncMock()
        fake_client.__aenter__.return_value.post = AsyncMock(
            return_value=fake_response
        )

        with patch("src.modules.notifications.channels.settings") as mock_settings, \
             patch("src.modules.notifications.channels.httpx.AsyncClient", return_value=fake_client):
            mock_settings.SMTP_ENABLED = False
            mock_settings.RESEND_ENABLED = True
            mock_settings.RESEND_API_KEY = SecretStr("re_test_key")
            mock_settings.RESEND_EMAIL = "noreply@qlockcare.com"
            mock_settings.RESEND_API_TIMEOUT_SECONDS = 10
            mock_settings.SMTP_FROM_NAME = "QlockCare"
            mock_settings.SMTP_FROM_EMAIL = "noreply@qlockcare.local"
            provider = EmailProvider()
            result = await provider.send(
                to="x@example.com", subject="Hi", body="There"
            )

        assert result.success is True
        assert result.provider_message_id == "resend:abc123"
        # Confirm the call hit Resend's API with the expected payload.
        post_call = fake_client.__aenter__.return_value.post.await_args
        assert post_call.args[0] == "https://api.resend.com/emails"
        assert post_call.kwargs["headers"]["Authorization"] == "Bearer re_test_key"
        assert post_call.kwargs["json"]["from"] == "QlockCare <noreply@qlockcare.com>"
        assert post_call.kwargs["json"]["to"] == ["x@example.com"]
        assert post_call.kwargs["json"]["subject"] == "Hi"
        assert post_call.kwargs["json"]["text"] == "There"

    async def test_resend_non_2xx_returns_failure(self) -> None:
        from pydantic import SecretStr

        fake_response = MagicMock()
        fake_response.status_code = 422
        fake_response.text = "validation failed"

        fake_client = AsyncMock()
        fake_client.__aenter__.return_value.post = AsyncMock(
            return_value=fake_response
        )

        with patch("src.modules.notifications.channels.settings") as mock_settings, \
             patch("src.modules.notifications.channels.httpx.AsyncClient", return_value=fake_client):
            mock_settings.SMTP_ENABLED = False
            mock_settings.RESEND_ENABLED = True
            mock_settings.RESEND_API_KEY = SecretStr("re_test_key")
            mock_settings.RESEND_EMAIL = "noreply@qlockcare.com"
            mock_settings.RESEND_API_TIMEOUT_SECONDS = 10
            mock_settings.SMTP_FROM_NAME = "QlockCare"
            mock_settings.SMTP_FROM_EMAIL = "noreply@qlockcare.local"
            provider = EmailProvider()
            result = await provider.send(
                to="x@example.com", subject="Hi", body="There"
            )

        assert result.success is False
        assert "422" in (result.error or "")
        assert "validation failed" in (result.error or "")

    async def test_resend_enabled_overrides_smtp(self) -> None:
        """When both flags are on, Resend wins over SMTP."""
        from pydantic import SecretStr

        fake_response = MagicMock()
        fake_response.status_code = 201
        fake_response.json.return_value = {"id": "id-1"}
        fake_response.text = "{}"

        fake_client = AsyncMock()
        fake_client.__aenter__.return_value.post = AsyncMock(
            return_value=fake_response
        )

        with patch("src.modules.notifications.channels.settings") as mock_settings, \
             patch("src.modules.notifications.channels.httpx.AsyncClient", return_value=fake_client), \
             patch("src.modules.notifications.channels.aiosmtplib.send", new=AsyncMock()) as smtp_send:
            mock_settings.SMTP_ENABLED = True
            mock_settings.SMTP_HOST = "localhost"
            mock_settings.SMTP_PORT = 1025
            mock_settings.SMTP_USERNAME = ""
            mock_settings.SMTP_PASSWORD = None
            mock_settings.SMTP_USE_TLS = False
            mock_settings.SMTP_TIMEOUT_SECONDS = 10
            mock_settings.RESEND_ENABLED = True
            mock_settings.RESEND_API_KEY = SecretStr("re_test_key")
            mock_settings.RESEND_EMAIL = "noreply@qlockcare.com"
            mock_settings.RESEND_API_TIMEOUT_SECONDS = 10
            mock_settings.SMTP_FROM_NAME = "QlockCare"
            mock_settings.SMTP_FROM_EMAIL = "noreply@qlockcare.local"
            provider = EmailProvider()
            result = await provider.send(
                to="x@example.com", subject="Hi", body="There"
            )

        assert result.success is True
        assert result.provider_message_id == "resend:id-1"
        smtp_send.assert_not_called()

    async def test_resend_disabled_falls_back_to_smtp(self) -> None:
        """RESEND_ENABLED=false + SMTP_ENABLED=true → SMTP branch."""
        with patch("src.modules.notifications.channels.settings") as mock_settings, \
             patch("src.modules.notifications.channels.aiosmtplib.send", new=AsyncMock()) as smtp_send:
            mock_settings.SMTP_ENABLED = True
            mock_settings.SMTP_HOST = "localhost"
            mock_settings.SMTP_PORT = 1025
            mock_settings.SMTP_USERNAME = ""
            mock_settings.SMTP_PASSWORD = None
            mock_settings.SMTP_USE_TLS = False
            mock_settings.SMTP_TIMEOUT_SECONDS = 10
            mock_settings.RESEND_ENABLED = False
            mock_settings.RESEND_API_KEY = None
            mock_settings.SMTP_FROM_NAME = "QlockCare"
            mock_settings.SMTP_FROM_EMAIL = "noreply@qlockcare.local"
            provider = EmailProvider()
            result = await provider.send(
                to="x@example.com", subject="Hi", body="There"
            )

        assert result.success is True
        smtp_send.assert_awaited_once()

    async def test_resend_network_error_returns_failure(self) -> None:
        from pydantic import SecretStr

        fake_client = AsyncMock()
        fake_client.__aenter__.return_value.post = AsyncMock(
            side_effect=RuntimeError("connection refused")
        )

        with patch("src.modules.notifications.channels.settings") as mock_settings, \
             patch("src.modules.notifications.channels.httpx.AsyncClient", return_value=fake_client):
            mock_settings.SMTP_ENABLED = False
            mock_settings.RESEND_ENABLED = True
            mock_settings.RESEND_API_KEY = SecretStr("re_test_key")
            mock_settings.RESEND_EMAIL = "noreply@qlockcare.com"
            mock_settings.RESEND_API_TIMEOUT_SECONDS = 10
            mock_settings.SMTP_FROM_NAME = "QlockCare"
            mock_settings.SMTP_FROM_EMAIL = "noreply@qlockcare.local"
            provider = EmailProvider()
            result = await provider.send(
                to="x@example.com", subject="Hi", body="There"
            )

        assert result.success is False
        assert "connection refused" in (result.error or "")

    def test_channel_is_email(self) -> None:
        assert EmailProvider.channel == NotificationChannel.EMAIL


class TestSMSProvider:
    def test_stub_mode_does_not_raise(self) -> None:
        with patch("src.modules.notifications.channels.settings") as mock_settings:
            mock_settings.SMS_ENABLED = False
            provider = SMSProvider()
            assert provider is not None

    def test_enabled_without_creds_raises(self) -> None:
        with patch("src.modules.notifications.channels.settings") as mock_settings:
            mock_settings.SMS_ENABLED = True
            mock_settings.TWILIO_ACCOUNT_SID = None
            mock_settings.TWILIO_AUTH_TOKEN = None
            mock_settings.TWILIO_FROM_NUMBER = None
            with pytest.raises(NotImplementedError) as excinfo:
                SMSProvider()
            assert "Twilio" in str(excinfo.value)

    def test_enabled_with_partial_creds_raises(self) -> None:
        with patch("src.modules.notifications.channels.settings") as mock_settings:
            mock_settings.SMS_ENABLED = True
            mock_settings.TWILIO_ACCOUNT_SID = "AC123"
            mock_settings.TWILIO_AUTH_TOKEN = None  # missing
            mock_settings.TWILIO_FROM_NUMBER = "+15551234567"
            with pytest.raises(NotImplementedError) as excinfo:
                SMSProvider()
            assert "TWILIO_AUTH_TOKEN" in str(excinfo.value)

    async def test_stub_send_returns_success(self) -> None:
        with patch("src.modules.notifications.channels.settings") as mock_settings:
            mock_settings.SMS_ENABLED = False
            provider = SMSProvider()
            result = await provider.send(
                to="+15551234567", subject="Hi", body="There"
            )
            assert result.success is True
            assert result.provider_message_id is not None
            assert "sms-stub:" in result.provider_message_id

    def test_channel_is_sms(self) -> None:
        assert SMSProvider.channel == NotificationChannel.SMS


class TestProviderRegistry:
    def setup_method(self) -> None:
        # Reset registry between tests so cached providers don't leak.
        ProviderRegistry._PROVIDERS = {}

    def test_in_app_always_available(self) -> None:
        provider = ProviderRegistry.get(NotificationChannel.IN_APP)
        assert provider is not None
        assert provider.channel == NotificationChannel.IN_APP

    def test_sms_stub_always_available(self) -> None:
        with patch("src.modules.notifications.channels.settings") as mock_settings:
            mock_settings.SMS_ENABLED = False
            provider = ProviderRegistry.get(NotificationChannel.SMS)
            assert provider is not None

    def test_email_disabled_returns_none(self) -> None:
        with patch("src.modules.notifications.channels.settings") as mock_settings:
            mock_settings.SMTP_ENABLED = False
            mock_settings.RESEND_ENABLED = False
            mock_settings.RESEND_API_KEY = None
            provider = ProviderRegistry.get(NotificationChannel.EMAIL)
            assert provider is None

    def test_email_enabled_returns_provider(self) -> None:
        with patch("src.modules.notifications.channels.settings") as mock_settings:
            mock_settings.SMTP_ENABLED = True
            mock_settings.SMTP_HOST = "localhost"
            mock_settings.SMTP_PORT = 1025
            mock_settings.SMTP_USERNAME = ""
            mock_settings.SMTP_PASSWORD = None
            mock_settings.SMTP_FROM_NAME = "QlockCare"
            mock_settings.SMTP_FROM_EMAIL = "noreply@qlockcare.local"
            mock_settings.SMTP_USE_TLS = False
            mock_settings.RESEND_ENABLED = False
            mock_settings.RESEND_API_KEY = None
            provider = ProviderRegistry.get(NotificationChannel.EMAIL)
            assert provider is not None
            assert provider.channel == NotificationChannel.EMAIL

    def test_email_resend_enabled_returns_provider(self) -> None:
        with patch("src.modules.notifications.channels.settings") as mock_settings:
            mock_settings.SMTP_ENABLED = False
            mock_settings.RESEND_ENABLED = True
            mock_settings.RESEND_API_KEY = "re_test_key"
            provider = ProviderRegistry.get(NotificationChannel.EMAIL)
            assert provider is not None
            assert provider.channel == NotificationChannel.EMAIL

    def test_push_not_wired(self) -> None:
        provider = ProviderRegistry.get(NotificationChannel.PUSH)
        assert provider is None

    def test_enabled_channels_default_env(self) -> None:
        with patch("src.modules.notifications.channels.settings") as mock_settings:
            mock_settings.SMTP_ENABLED = False
            mock_settings.RESEND_ENABLED = False
            mock_settings.RESEND_API_KEY = None
            mock_settings.SMS_ENABLED = False
            # Reset to ensure clean state.
            ProviderRegistry._PROVIDERS = {}
            channels = ProviderRegistry.enabled_channels()
            assert NotificationChannel.IN_APP in channels
            assert NotificationChannel.SMS in channels
            assert NotificationChannel.EMAIL not in channels
            assert NotificationChannel.PUSH not in channels

    def test_cached_across_calls(self) -> None:
        p1 = ProviderRegistry.get(NotificationChannel.IN_APP)
        p2 = ProviderRegistry.get(NotificationChannel.IN_APP)
        assert p1 is p2


class TestNotificationProviderABC:
    def test_cannot_instantiate_abstract(self) -> None:
        with pytest.raises(TypeError):
            NotificationProvider()  # type: ignore[abstract]
