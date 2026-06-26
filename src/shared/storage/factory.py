"""Storage adapter factory.

`get_storage()` returns the singleton configured for the current environment.
Cached for the process lifetime.
"""

from __future__ import annotations

from functools import lru_cache

from src.core.config import settings
from src.core.exceptions import ServiceUnavailableError
from src.shared.storage.base import StorageAdapter
from src.shared.storage.s3_adapter import S3StorageAdapter
from src.shared.storage.supabase_adapter import SupabaseStorageAdapter


@lru_cache(maxsize=1)
def get_storage() -> StorageAdapter:
    """Return the storage adapter for the configured backend.

    Choices:
    - `s3` (default) — S3StorageAdapter against AWS / Floci / MinIO / R2
    - `supabase` — SupabaseStorageAdapter

    Raises:
        ServiceUnavailableError: if required env vars are missing.
    """
    backend = settings.STORAGE_BACKEND

    if backend == "s3":
        return S3StorageAdapter(
            endpoint_url=settings.S3_ENDPOINT_URL,
            region=settings.S3_REGION,
            access_key=settings.S3_ACCESS_KEY_ID.get_secret_value(),
            secret_key=settings.S3_SECRET_ACCESS_KEY.get_secret_value(),
            force_path_style=settings.S3_FORCE_PATH_STYLE,
        )

    if backend == "supabase":
        if not settings.SUPABASE_URL or not settings.SUPABASE_SERVICE_ROLE_KEY:
            raise ServiceUnavailableError(
                "Supabase storage backend requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY"
            )
        return SupabaseStorageAdapter(
            url=settings.SUPABASE_URL,
            service_key=settings.SUPABASE_SERVICE_ROLE_KEY.get_secret_value(),
            default_bucket=settings.SUPABASE_STORAGE_BUCKET_QUALIFICATIONS,
        )

    raise ServiceUnavailableError(f"Unknown STORAGE_BACKEND: {backend}")


__all__ = ["get_storage"]
