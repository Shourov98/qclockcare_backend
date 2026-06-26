"""Storage adapter abstract base.

Implementations:
- `S3StorageAdapter` (default) — any S3-compatible API
- `SupabaseStorageAdapter` — Supabase Storage
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class UploadResult:
    """The outcome of a successful upload."""

    storage_key: str  # the opaque key inside the bucket
    etag: str | None
    size_bytes: int


class StorageAdapter(ABC):
    """File storage interface. See `26_LOCAL_STORAGE_AND_FLOCI.md` for design."""

    @abstractmethod
    def upload(
        self,
        *,
        bucket: str,
        key: str,
        body: bytes,
        content_type: str,
        metadata: dict[str, str] | None = None,
    ) -> UploadResult:
        """Upload an object. Returns the storage key + metadata."""

    @abstractmethod
    def download(self, *, bucket: str, key: str) -> bytes:
        """Download an object's full contents. Raises on missing object."""

    @abstractmethod
    def presigned_url(
        self,
        *,
        bucket: str,
        key: str,
        expires_in: int = 900,
        method: str = "GET",
    ) -> str:
        """Generate a time-limited URL for direct client access.

        `method="PUT"` for uploads, `"GET"` for downloads.
        """

    @abstractmethod
    def delete(self, *, bucket: str, key: str) -> None:
        """Delete an object. No-op if it doesn't exist."""

    @abstractmethod
    def exists(self, *, bucket: str, key: str) -> bool:
        """True iff the object is present in the bucket."""

    @abstractmethod
    def list_objects(
        self,
        *,
        bucket: str,
        prefix: str = "",
        max_keys: int = 1000,
    ) -> list[dict[str, Any]]:
        """List objects in a bucket, optionally filtered by key prefix.

        Returns a list of dicts with keys: `key`, `size`, `last_modified`, `etag`.
        """
