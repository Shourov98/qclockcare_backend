"""Unit tests for `staff_service.build_download_url` and the
`StaffQualificationResponse` shape that the new download surface
relies on.

These tests focus on what's testable without spinning up a DB session
or TestClient:

1. The `QualificationDownloadResponse` schema enforces the
   `expires_in` bounds (60..86400) declared by `S3_PRESIGNED_URL_TTL_SECONDS`.
2. `StaffQualificationResponse.download_url` / `expires_in` are
   `Optional` and default to `None`, matching the no-document case.
3. The router-level `_qualification_to_response` helper builds a
   proper response — populates `download_url` + `expires_in` when a
   document is attached, leaves them `None` otherwise. We patch
   `staff_service.build_download_url` to avoid any storage I/O.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _fake_qual(*, document_storage_key: str | None) -> SimpleNamespace:
    """Build a SimpleNamespace that quacks like `StaffQualification`."""
    return SimpleNamespace(
        id=uuid4(),
        staff_id=uuid4(),
        agency_id=uuid4(),
        qualification_type="CPR",
        program_type=None,
        document_storage_key=document_storage_key,
        issued_at=date(2025, 1, 1),
        expires_at=date(2027, 1, 1),
        status="ACTIVE",
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
        updated_at=datetime(2025, 1, 1, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# QualificationDownloadResponse schema
# ---------------------------------------------------------------------------
class TestQualificationDownloadResponse:
    def test_happy_path(self) -> None:
        from src.modules.staff.schemas import QualificationDownloadResponse

        resp = QualificationDownloadResponse(
            download_url="https://bucket.example/x?signature=abc",
            expires_in=900,
            expires_at=datetime(2026, 6, 28, 16, 0, tzinfo=UTC),
        )
        assert resp.download_url.startswith("https://")
        assert resp.expires_in == 900
        assert resp.expires_at.tzinfo is not None

    def test_expires_in_below_minimum_rejected(self) -> None:
        from src.modules.staff.schemas import QualificationDownloadResponse

        with pytest.raises(ValidationError):
            QualificationDownloadResponse(
                download_url="https://x",
                expires_in=10,  # below the 60-second floor
                expires_at=datetime(2026, 6, 28, tzinfo=UTC),
            )

    def test_expires_in_above_maximum_rejected(self) -> None:
        from src.modules.staff.schemas import QualificationDownloadResponse

        with pytest.raises(ValidationError):
            QualificationDownloadResponse(
                download_url="https://x",
                expires_in=999_999,  # above the 24h ceiling
                expires_at=datetime(2026, 6, 28, tzinfo=UTC),
            )


# ---------------------------------------------------------------------------
# StaffQualificationResponse — download_url / expires_in defaults
# ---------------------------------------------------------------------------
class TestStaffQualificationResponse:
    def test_download_url_defaults_to_none(self) -> None:
        """When the qualification has no attached document, both
        fields are None — the raw storage key is never exposed."""
        from src.modules.staff.schemas import StaffQualificationResponse

        qual = _fake_qual(document_storage_key=None)
        resp = StaffQualificationResponse.model_validate(qual)
        assert resp.download_url is None
        assert resp.expires_in is None

    def test_download_url_required_string_when_populated(self) -> None:
        from src.modules.staff.schemas import StaffQualificationResponse

        with pytest.raises(ValidationError):
            StaffQualificationResponse(
                id=uuid4(),
                staff_id=uuid4(),
                agency_id=uuid4(),
                qualification_type="CPR",
                program_type=None,
                download_url=123,  # type: ignore[arg-type]
                expires_in=900,
                issued_at=None,
                expires_at=None,
                status="ACTIVE",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )

    def test_expires_in_must_be_int(self) -> None:
        """`expires_in` is a plain int — bounds are enforced by
        `QualificationDownloadResponse` (the dedicated download
        endpoint), not the list response."""
        from src.modules.staff.schemas import StaffQualificationResponse

        resp = StaffQualificationResponse(
            id=uuid4(),
            staff_id=uuid4(),
            agency_id=uuid4(),
            qualification_type="CPR",
            program_type=None,
            download_url="https://x",
            expires_in=900,
            issued_at=None,
            expires_at=None,
            status="ACTIVE",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        assert resp.expires_in == 900


# ---------------------------------------------------------------------------
# _qualification_to_response — router helper
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestQualificationToResponse:
    async def test_populates_download_url_when_document_attached(self) -> None:
        """`_qualification_to_response` must call
        `staff_service.build_download_url` and copy the URL +
        `expires_in` into the response."""
        from src.modules.staff import router as staff_router

        qual = _fake_qual(document_storage_key="cpr/alice.pdf")
        fake_url = "https://signed.example/cpr/alice.pdf?X-Amz-Signature=abc"

        with (
            patch.object(
                staff_router.staff_service,
                "build_download_url",
                AsyncMock(return_value=(fake_url, datetime(2026, 6, 28, 16, 0, tzinfo=UTC))),
            ),
            patch.object(staff_router, "settings") as mock_settings,
        ):
            mock_settings.S3_PRESIGNED_URL_TTL_SECONDS = 900
            resp = await staff_router._qualification_to_response(qual)

        assert resp.download_url == fake_url
        assert resp.expires_in == 900
        assert resp.id == qual.id
        assert resp.qualification_type == qual.qualification_type

    async def test_no_document_leaves_fields_none(self) -> None:
        """When `document_storage_key` is None, both `download_url`
        and `expires_in` must stay None — and
        `build_download_url` is never called (avoids unnecessary
        calls to the storage adapter on the list endpoint)."""
        from src.modules.staff import router as staff_router

        qual = _fake_qual(document_storage_key=None)
        with patch.object(
            staff_router.staff_service,
            "build_download_url",
            AsyncMock(),
        ) as mock_build:
            resp = await staff_router._qualification_to_response(qual)

        assert resp.download_url is None
        assert resp.expires_in is None
        mock_build.assert_not_called()


# ---------------------------------------------------------------------------
# build_download_url — validation edge cases (no FakeStorageAdapter needed)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestBuildDownloadUrlValidation:
    async def test_empty_string_storage_key_rejected(self) -> None:
        from src.core.exceptions import ValidationError as DomainValidationError
        from src.modules.staff import service as staff_service

        with pytest.raises(DomainValidationError):
            await staff_service.build_download_url(storage_key="")

    async def test_none_storage_key_rejected(self) -> None:
        """The router uses `qual.document_storage_key or ""` as a
        defence-in-depth. Verify that even if someone forgets the
        `or ""`, an empty string is what gets passed in."""
        from src.core.exceptions import ValidationError as DomainValidationError
        from src.modules.staff import service as staff_service

        # `build_download_url` types storage_key as `str` (required),
        # so passing None would surface as a TypeError before
        # ValidationError. Accept either — the contract is "don't
        # call this with a missing document".
        with pytest.raises((DomainValidationError, TypeError)):
            await staff_service.build_download_url(storage_key=None)  # type: ignore[arg-type]


__all__ = [
    "TestBuildDownloadUrlValidation",
    "TestQualificationDownloadResponse",
    "TestQualificationToResponse",
    "TestStaffQualificationResponse",
]
