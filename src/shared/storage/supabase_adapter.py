"""Supabase Storage adapter.

Alternative backend for teams that already use Supabase Storage. Production
deployments should prefer the S3 adapter for portability and presigned-URL
flexibility.
"""

from __future__ import annotations

from typing import Any

from supabase import Client, create_client

from src.shared.storage.base import StorageAdapter, UploadResult


class SupabaseStorageAdapter(StorageAdapter):
    """Adapter for Supabase Storage.

    Supabase Storage has its own auth model and supports signed URLs natively.
    Presigned URLs use the `create_signed_url` method.
    """

    def __init__(self, *, url: str, service_key: str, default_bucket: str) -> None:
        self._client: Client = create_client(url, service_key)
        self._default_bucket = default_bucket

    def _bucket(self, bucket: str | None) -> Any:
        bucket_name = bucket or self._default_bucket
        return self._client.storage.from_(bucket_name)

    def upload(
        self,
        *,
        bucket: str,
        key: str,
        body: bytes,
        content_type: str,
        metadata: dict[str, str] | None = None,
    ) -> UploadResult:
        options: dict[str, Any] = {"content-type": content_type}
        if metadata:
            options["metadata"] = metadata
        # Supabase returns a dict; success path is `Key` present.
        self._bucket(bucket).upload(key, body, file_options=options)
        return UploadResult(storage_key=key, etag=None, size_bytes=len(body))

    def download(self, *, bucket: str, key: str) -> bytes:
        resp = self._bucket(bucket).download(key)
        return resp if isinstance(resp, bytes) else bytes(resp)

    def presigned_url(
        self,
        *,
        bucket: str,
        key: str,
        expires_in: int = 900,
        method: str = "GET",
    ) -> str:
        # Supabase only supports GET signed URLs out of the box.
        # For PUT, use the direct upload path.
        resp: dict[str, Any] = self._bucket(bucket).create_signed_url(key, expires_in)
        return str(resp.get("signedURL", ""))

    def exists(self, *, bucket: str, key: str) -> bool:
        # List the bucket prefix and look for the key.
        items = self._bucket(bucket).list(path=key)
        return any(item.get("name") == key for item in items)

    def delete(self, *, bucket: str, key: str) -> None:
        self._bucket(bucket).remove([key])

    def list_objects(
        self,
        *,
        bucket: str,
        prefix: str = "",
        max_keys: int = 1000,
    ) -> list[dict[str, Any]]:
        items = self._bucket(bucket).list(path=prefix, limit=max_keys)
        return [
            {
                "key": item.get("name", ""),
                "size": item.get("metadata", {}).get("size", 0),
                "last_modified": item.get("updated_at"),
                "etag": None,
            }
            for item in items
        ]


__all__ = ["SupabaseStorageAdapter"]
