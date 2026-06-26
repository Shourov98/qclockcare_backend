"""S3-compatible storage layer (ADR-0018).

`StorageAdapter` is the abstract interface. `S3StorageAdapter` works against
any S3-compatible API — Floci (local dev), AWS S3, MinIO, Cloudflare R2.
`SupabaseStorageAdapter` is an alternative that uses Supabase Storage.

`get_storage()` is the factory; cached for the app lifetime.
"""

from src.shared.storage.base import StorageAdapter, UploadResult
from src.shared.storage.factory import get_storage

__all__ = ["StorageAdapter", "UploadResult", "get_storage"]
