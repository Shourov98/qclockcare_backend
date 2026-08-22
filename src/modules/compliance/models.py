"""Compliance ORM models.

Tables:
- `agency_documents` — required documents per agency (license, cert, etc.)
- `agency_licenses`  — expiring-license records per agency

These power the `/admin/compliance` endpoints. Both are scoped by
`agency_id` so RLS can apply once we turn it on.

`status` is stored (rather than derived) so the dashboard can index it
for fast filter queries. The service layer is responsible for keeping
it in sync with `expires_at` on every update.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.domain.base_entity import Base, IdMixin, SoftDeleteMixin, TimestampedMixin
from src.shared.domain.enum_mapping import pg_name
from src.shared.domain.enums import DocumentStatus, DocumentType, LicenseStatus

if TYPE_CHECKING:
    from src.modules.agencies.models import Agency


class AgencyDocument(IdMixin, TimestampedMixin, SoftDeleteMixin, Base):
    """A required per-agency document (license, cert, audit, manual, …).

    `status` is the lifecycle bucket the admin cares about for filtering:
      - `MISSING` — required by policy but not yet uploaded.
      - `PENDING` — uploaded; awaiting admin review.
      - `VALID` — uploaded + verified; expiry is comfortably far away.
      - `EXPIRING` — within `EXPIRING_SOON_DAYS` of `expires_at`.
      - `EXPIRED` — past `expires_at`.
      - `REJECTED` — uploaded but wrong type / illegible / etc.

    `extra` carries document-specific metadata (e.g. issuer, policy
    number, verification_url) without requiring a schema change.
    """

    __tablename__ = "agency_documents"

    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    doc_type: Mapped[DocumentType] = mapped_column(
        Enum(DocumentType, name=pg_name(DocumentType)),
        nullable=False,
        default=DocumentType.DOCUMENT,
        server_default=DocumentType.DOCUMENT.value,
        index=True,
    )
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, name=pg_name(DocumentStatus)),
        nullable=False,
        default=DocumentStatus.MISSING,
        server_default=DocumentStatus.MISSING.value,
        index=True,
    )

    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    # Optional pointer to a future file-upload blob. Nullable because v1
    # stores no file content — `MISSING` rows will have this set to NULL.
    file_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    extra: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    agency: Mapped["Agency"] = relationship()

    __table_args__ = (
        Index("idx_agency_documents_agency_status", "agency_id", "status"),
        Index("idx_agency_documents_expires_at", "expires_at"),
    )


class AgencyLicense(IdMixin, TimestampedMixin, SoftDeleteMixin, Base):
    """A license or certification that the agency must renew.

    Distinct from `AgencyDocument` because licenses have a hard expiry
    the dashboard cares about (Critical / Warning / Upcoming buckets in
    `ExpiringLicensesTable`) and a more constrained set of fields.

    `status` is derived from `expires_at` and refreshed on every write
    so the FE can filter without recomputing.
    """

    __tablename__ = "agency_licenses"

    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    doc_type: Mapped[DocumentType] = mapped_column(
        Enum(DocumentType, name=pg_name(DocumentType)),
        nullable=False,
        default=DocumentType.LICENSE,
        server_default=DocumentType.LICENSE.value,
        index=True,
    )
    status: Mapped[LicenseStatus] = mapped_column(
        Enum(LicenseStatus, name=pg_name(LicenseStatus)),
        nullable=False,
        default=LicenseStatus.VALID,
        server_default=LicenseStatus.VALID.value,
        index=True,
    )

    issued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    # Free-form fields the FE shows ("State Insurance License #12345",
    # "Policy number AX-9988", etc.).
    reference_number: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    extra: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    agency: Mapped["Agency"] = relationship()

    __table_args__ = (
        Index("idx_agency_licenses_agency_status", "agency_id", "status"),
        Index("idx_agency_licenses_status_expires", "status", "expires_at"),
    )


__all__ = ["AgencyDocument", "AgencyLicense"]
