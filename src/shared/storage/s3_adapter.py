"""S3-compatible storage adapter.

Works against any service speaking the S3 API:
- AWS S3 (production)
- Floci (local dev, default)
- MinIO
- Cloudflare R2

Switch providers with env vars only — no code changes.
"""

from __future__ import annotations

from typing import Any

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from src.shared.storage.base import StorageAdapter, UploadResult


class S3StorageAdapter(StorageAdapter):
    """boto3-backed adapter.

    `force_path_style` is `True` for Floci/MinIO/R2, `False` for AWS S3.
    """

    def __init__(
        self,
        *,
        endpoint_url: str | None,
        region: str,
        access_key: str,
        secret_key: str,
        force_path_style: bool,
    ) -> None:
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path" if force_path_style else "virtual"},
            ),
        )

    # ---- writes ----
    def upload(
        self,
        *,
        bucket: str,
        key: str,
        body: bytes,
        content_type: str,
        metadata: dict[str, str] | None = None,
    ) -> UploadResult:
        params: dict[str, Any] = {
            "Bucket": bucket,
            "Key": key,
            "Body": body,
            "ContentType": content_type,
        }
        if metadata:
            params["Metadata"] = metadata
        resp = self._client.put_object(**params)
        return UploadResult(
            storage_key=key,
            etag=resp.get("ETag"),
            size_bytes=len(body),
        )

    # ---- reads ----
    def download(self, *, bucket: str, key: str) -> bytes:
        resp = self._client.get_object(Bucket=bucket, Key=key)
        return resp["Body"].read()

    def presigned_url(
        self,
        *,
        bucket: str,
        key: str,
        expires_in: int = 900,
        method: str = "GET",
    ) -> str:
        op = "get_object" if method == "GET" else "put_object"
        return self._client.generate_presigned_url(
            op,
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires_in,
        )

    def exists(self, *, bucket: str, key: str) -> bool:
        try:
            self._client.head_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
        return True

    def delete(self, *, bucket: str, key: str) -> None:
        self._client.delete_object(Bucket=bucket, Key=key)

    def list_objects(
        self,
        *,
        bucket: str,
        prefix: str = "",
        max_keys: int = 1000,
    ) -> list[dict[str, Any]]:
        resp = self._client.list_objects_v2(
            Bucket=bucket,
            Prefix=prefix,
            MaxKeys=max_keys,
        )
        return [
            {
                "key": obj["Key"],
                "size": obj["Size"],
                "last_modified": obj["LastModified"].isoformat(),
                "etag": obj.get("ETag"),
            }
            for obj in resp.get("Contents", [])
        ]


__all__ = ["S3StorageAdapter"]
