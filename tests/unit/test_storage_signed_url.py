"""Unit tests for storage adapter `presigned_url` and the staff
service `build_download_url` helper.

These tests do NOT touch the network. They:

1. Verify `S3StorageAdapter.presigned_url` and
   `SupabaseStorageAdapter.presigned_url` pass `expires_in` through
   to their underlying clients unchanged.
2. Verify `staff_service.build_download_url` reads
   `settings.S3_PRESIGNED_URL_TTL_SECONDS` and resolves the right
   bucket based on `settings.STORAGE_BACKEND`.
3. Verify `build_download_url` raises `ValidationError` when the
   storage key is empty (qualification has no attached document).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.core.exceptions import ValidationError


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
class FakeStorageAdapter:
    """In-memory `StorageAdapter` stub that records calls.

    Implements only the methods touched by `build_download_url` —
    the abstract methods that we don't call raise so we notice if
    we accidentally start calling them.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def presigned_url(self, *, bucket, key, expires_in=900, method="GET"):
        self.calls.append(
            {
                "bucket": bucket,
                "key": key,
                "expires_in": expires_in,
                "method": method,
            }
        )
        # Distinct, recognisable URL so tests can assert on it.
        return f"https://fake.example/{bucket}/{key}?ttl={expires_in}&method={method}"

    # ---- unused abstract methods — should never be called here ----
    def upload(self, **_):
        raise AssertionError("upload() should not be called by build_download_url")

    def download(self, **_):
        raise AssertionError("download() should not be called by build_download_url")

    def delete(self, **_):
        raise AssertionError("delete() should not be called by build_download_url")

    def exists(self, **_):
        raise AssertionError("exists() should not be called by build_download_url")

    def list_objects(self, **_):
        raise AssertionError("list_objects() should not be called by build_download_url")


def _patched_settings(**overrides):
    """Return a `settings` stand-in with the given overrides applied
    on top of the real singleton."""
    from src.core.config import settings as real

    base = SimpleNamespace(
        STORAGE_BACKEND=real.STORAGE_BACKEND,
        S3_BUCKET_QUALIFICATIONS=real.S3_BUCKET_QUALIFICATIONS,
        SUPABASE_STORAGE_BUCKET_QUALIFICATIONS=real.SUPABASE_STORAGE_BUCKET_QUALIFICATIONS,
        S3_PRESIGNED_URL_TTL_SECONDS=real.S3_PRESIGNED_URL_TTL_SECONDS,
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


# ---------------------------------------------------------------------------
# S3StorageAdapter — passes expires_in through to boto3
# ---------------------------------------------------------------------------
class TestS3PresignedUrlPassThrough:
    def test_passes_expires_in_unchanged(self) -> None:
        """`S3StorageAdapter.presigned_url` must forward the caller's
        `expires_in` argument verbatim to
        `client.generate_presigned_url` — no hard-coding, no
        clamping."""
        from src.shared.storage.s3_adapter import S3StorageAdapter

        adapter = S3StorageAdapter(
            endpoint_url=None,
            region="us-east-1",
            access_key="AKIA",
            secret_key="secret",
            force_path_style=True,
        )
        fake_client = MagicMock()
        fake_client.generate_presigned_url.return_value = "https://signed.example/x"
        adapter._client = fake_client  # bypass the real boto3 client

        url = adapter.presigned_url(
            bucket="quals",
            key="cpr/alice.pdf",
            expires_in=1800,
            method="GET",
        )

        assert url == "https://signed.example/x"
        fake_client.generate_presigned_url.assert_called_once_with(
            "get_object",
            Params={"Bucket": "quals", "Key": "cpr/alice.pdf"},
            ExpiresIn=1800,
        )

    def test_put_method_maps_to_put_object(self) -> None:
        from src.shared.storage.s3_adapter import S3StorageAdapter

        adapter = S3StorageAdapter(
            endpoint_url=None,
            region="us-east-1",
            access_key="AKIA",
            secret_key="secret",
            force_path_style=False,
        )
        fake_client = MagicMock()
        fake_client.generate_presigned_url.return_value = "https://signed.example/y"
        adapter._client = fake_client

        adapter.presigned_url(
            bucket="quals",
            key="cpr/alice.pdf",
            expires_in=300,
            method="PUT",
        )

        op = fake_client.generate_presigned_url.call_args.args[0]
        assert op == "put_object"


# ---------------------------------------------------------------------------
# SupabaseStorageAdapter — passes expires_in through
# ---------------------------------------------------------------------------
class TestSupabasePresignedUrlPassThrough:
    def test_passes_expires_in_unchanged(self) -> None:
        """`SupabaseStorageAdapter.presigned_url` must forward
        `expires_in` to `create_signed_url` unchanged."""
        from src.shared.storage.supabase_adapter import SupabaseStorageAdapter

        adapter = SupabaseStorageAdapter(
            url="http://supabase.local",
            service_key="fake-key",
            default_bucket="quals",
        )
        fake_bucket = MagicMock()
        fake_bucket.create_signed_url.return_value = {
            "signedURL": "https://supabase.example/signed/x?token=abc"
        }
        fake_storage = MagicMock()
        fake_storage.from_.return_value = fake_bucket
        fake_client = MagicMock()
        fake_client.storage = fake_storage
        adapter._client = fake_client

        url = adapter.presigned_url(
            bucket="quals",
            key="cpr/alice.pdf",
            expires_in=1800,
            method="GET",
        )

        assert url == "https://supabase.example/signed/x?token=abc"
        fake_bucket.create_signed_url.assert_called_once_with(
            "cpr/alice.pdf", 1800
        )


# ---------------------------------------------------------------------------
# build_download_url — reads settings, picks bucket, calls adapter
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestBuildDownloadUrl:
    async def test_uses_settings_ttl_and_s3_bucket(self) -> None:
        from src.modules.staff import service as staff_service

        fake = FakeStorageAdapter()
        with (
            patch.object(staff_service, "get_storage", return_value=fake),
            patch.object(staff_service, "settings", _patched_settings(
                STORAGE_BACKEND="s3",
                S3_PRESIGNED_URL_TTL_SECONDS=900,
                S3_BUCKET_QUALIFICATIONS="agency-quals",
            )),
        ):
            url, expires_at = await staff_service.build_download_url(
                storage_key="cpr/alice.pdf"
            )

        assert fake.calls == [
            {
                "bucket": "agency-quals",
                "key": "cpr/alice.pdf",
                "expires_in": 900,
                "method": "GET",
            }
        ]
        assert url == "https://fake.example/agency-quals/cpr/alice.pdf?ttl=900&method=GET"
        # expires_at ~ now + 900s
        delta = (expires_at - datetime.now(UTC)).total_seconds()
        assert 895 <= delta <= 905

    async def test_uses_supabase_bucket_when_backend_supabase(self) -> None:
        from src.modules.staff import service as staff_service

        fake = FakeStorageAdapter()
        with (
            patch.object(staff_service, "get_storage", return_value=fake),
            patch.object(staff_service, "settings", _patched_settings(
                STORAGE_BACKEND="supabase",
                SUPABASE_STORAGE_BUCKET_QUALIFICATIONS="sb-quals",
                S3_PRESIGNED_URL_TTL_SECONDS=1800,
            )),
        ):
            url, _ = await staff_service.build_download_url(
                storage_key="cpr/bob.pdf"
            )

        assert fake.calls[0]["bucket"] == "sb-quals"
        assert fake.calls[0]["expires_in"] == 1800
        assert "sb-quals/cpr/bob.pdf" in url

    async def test_empty_storage_key_raises_validation_error(self) -> None:
        from src.modules.staff import service as staff_service

        fake = FakeStorageAdapter()
        with (  # noqa: SIM117
            patch.object(staff_service, "get_storage", return_value=fake),
            patch.object(staff_service, "settings", _patched_settings()),
        ):
            with pytest.raises(ValidationError) as ei:
                await staff_service.build_download_url(storage_key="")

        assert "document" in str(ei.value).lower()
        # Adapter was never called.
        assert fake.calls == []

    async def test_custom_ttl_setting_is_honoured(self) -> None:
        """Operators can change the TTL via settings without touching
        code. `build_download_url` must respect the live value."""
        from src.modules.staff import service as staff_service

        fake = FakeStorageAdapter()
        with (
            patch.object(staff_service, "get_storage", return_value=fake),
            patch.object(staff_service, "settings", _patched_settings(
                S3_PRESIGNED_URL_TTL_SECONDS=3600,
            )),
        ):
            await staff_service.build_download_url(storage_key="x.pdf")

        assert fake.calls[0]["expires_in"] == 3600


__all__ = [
    "TestBuildDownloadUrl",
    "TestS3PresignedUrlPassThrough",
    "TestSupabasePresignedUrlPassThrough",
]
