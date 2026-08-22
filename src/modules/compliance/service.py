"""Compliance service — documents + licenses CRUD + stats.

Status fields are derived from `expires_at` whenever we write a row, so
the FE can filter on `status` without recomputing on every read.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import NotFoundError
from src.modules.agencies.models import Agency
from src.modules.compliance.models import AgencyDocument, AgencyLicense
from src.shared.domain.enums import DocumentStatus, DocumentType, LicenseStatus

# Buckets used by both the service layer and the FE Critical/Warning/
# Upcoming logic in `ExpiringLicensesTable`.
EXPIRING_SOON_DAYS = 30   # anything inside 30d → EXPIRING / WARNING
WARNING_DAYS = 30         # 14d-30d
CRITICAL_DAYS = 14        # <14d OR past


def compute_document_status(expires_at: datetime | None) -> DocumentStatus:
    """Derive a `DocumentStatus` from `expires_at` (UTC, tz-aware)."""
    if expires_at is None:
        return DocumentStatus.VALID
    now = datetime.now(tz=UTC)
    if expires_at <= now:
        return DocumentStatus.EXPIRED
    if expires_at <= now + timedelta(days=EXPIRING_SOON_DAYS):
        return DocumentStatus.EXPIRING
    return DocumentStatus.VALID


def compute_license_status(expires_at: datetime) -> LicenseStatus:
    """Derive a `LicenseStatus` from `expires_at` (UTC, tz-aware)."""
    now = datetime.now(tz=UTC)
    if expires_at <= now:
        return LicenseStatus.EXPIRED
    days = (expires_at - now).days
    if days <= CRITICAL_DAYS:
        return LicenseStatus.CRITICAL
    if days <= WARNING_DAYS:
        return LicenseStatus.WARNING
    if days <= 60:
        return LicenseStatus.UPCOMING
    return LicenseStatus.VALID


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
async def _assert_agency_exists(session: AsyncSession, agency_id: uuid.UUID) -> Agency:
    agency = (
        await session.execute(
            select(Agency).where(
                Agency.id == agency_id, Agency.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if agency is None:
        raise NotFoundError(
            message="Agency not found.",
            details={"agency_id": str(agency_id), "resource": "agency"},
        )
    return agency


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------
async def list_documents(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
    agency_id: uuid.UUID | None = None,
    doc_type: DocumentStatus | DocumentType | None = None,
    status_filter: DocumentStatus | None = None,
    search: str | None = None,
    include_deleted: bool = False,
) -> tuple[list[AgencyDocument], int]:
    """Return a page of documents and the total count.

    `doc_type` and `status_filter` are mutually exclusive at the type
    level — callers should not pass both. We always pass the explicit
    `status_filter` when filtering by status.
    """
    filters = []
    if not include_deleted:
        filters.append(AgencyDocument.deleted_at.is_(None))
    if agency_id is not None:
        filters.append(AgencyDocument.agency_id == agency_id)
    if status_filter is not None:
        filters.append(AgencyDocument.status == status_filter)
    if search:
        pattern = f"%{search.strip()}%"
        filters.append(
            or_(
                AgencyDocument.name.ilike(pattern),
                AgencyDocument.description.ilike(pattern),
            )
        )

    count_stmt = select(func.count()).select_from(AgencyDocument)
    if filters:
        count_stmt = count_stmt.where(and_(*filters))
    total = (await session.execute(count_stmt)).scalar_one()

    list_stmt = (
        select(AgencyDocument)
        .order_by(AgencyDocument.created_at.desc(), AgencyDocument.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    if filters:
        list_stmt = list_stmt.where(and_(*filters))

    rows = (await session.execute(list_stmt)).scalars().all()
    return list(rows), total


async def get_document(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    include_deleted: bool = False,
) -> AgencyDocument:
    filters = [AgencyDocument.id == document_id]
    if not include_deleted:
        filters.append(AgencyDocument.deleted_at.is_(None))
    row = (
        await session.execute(select(AgencyDocument).where(*filters))
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(
            message="Document not found.",
            details={"document_id": str(document_id), "resource": "agency_document"},
        )
    return row


async def create_document(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    name: str,
    doc_type: DocumentType = DocumentType.DOCUMENT,
    status: DocumentStatus = DocumentStatus.MISSING,
    description: str | None = None,
    expires_at: datetime | None = None,
    file_url: str | None = None,
) -> AgencyDocument:
    """Insert a new required-document record."""
    await _assert_agency_exists(session, agency_id)
    row = AgencyDocument(
        agency_id=agency_id,
        name=name,
        doc_type=doc_type,
        status=status,
        description=description,
        expires_at=expires_at,
        file_url=file_url,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def update_document(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    changes: dict[str, Any],
) -> AgencyDocument:
    """Patch a document. Recomputes `status` when `expires_at` changes."""
    row = await get_document(session, document_id=document_id, include_deleted=True)
    for key, value in changes.items():
        if hasattr(row, key):
            setattr(row, key, value)
    # Refresh derived status if expiry moved.
    if "expires_at" in changes or "status" not in changes:
        # Don't override user-supplied status if they explicitly set one.
        if "status" not in changes:
            row.status = compute_document_status(row.expires_at)
    await session.commit()
    await session.refresh(row)
    return row


async def soft_delete_document(
    session: AsyncSession, *, document_id: uuid.UUID
) -> None:
    row = await get_document(session, document_id=document_id, include_deleted=True)
    if row.deleted_at is not None:
        return
    row.deleted_at = datetime.now(tz=UTC)
    await session.commit()


# --------------------------------------------------------------------------
# Missing documents report
# --------------------------------------------------------------------------
async def list_missing_documents(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
    search: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Return rows of `{agency, missing_count, documents}`.

    One row per agency that has ≥1 `MISSING` document. Documents are
    capped at 50 per agency so the FE can render the row inline without
    a second roundtrip.
    """
    # Find agency IDs with missing docs.
    base = (
        select(
            AgencyDocument.agency_id,
            func.count(AgencyDocument.id).label("missing_count"),
        )
        .where(
            AgencyDocument.deleted_at.is_(None),
            AgencyDocument.status == DocumentStatus.MISSING,
        )
        .group_by(AgencyDocument.agency_id)
    )
    if search:
        # Search by agency name — pull agencies that match.
        pattern = f"%{search.strip()}%"
        agency_sub = (
            select(Agency.id).where(Agency.deleted_at.is_(None), Agency.name.ilike(pattern))
        )
        base = base.where(AgencyDocument.agency_id.in_(agency_sub))
    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        await session.execute(
            base.order_by(func.count(AgencyDocument.id).desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    if not rows:
        return [], total

    agency_ids = [r.agency_id for r in rows]
    agencies = (
        await session.execute(
            select(Agency).where(Agency.id.in_(agency_ids))
        )
    ).scalars().all()
    name_by_id = {a.id: a.name for a in agencies}

    # Fetch up to 50 missing docs per agency.
    docs_by_agency: dict[uuid.UUID, list[AgencyDocument]] = {aid: [] for aid in agency_ids}
    doc_rows = (
        await session.execute(
            select(AgencyDocument)
            .where(
                AgencyDocument.deleted_at.is_(None),
                AgencyDocument.agency_id.in_(agency_ids),
                AgencyDocument.status == DocumentStatus.MISSING,
            )
            .order_by(AgencyDocument.created_at.desc(), AgencyDocument.id)
            .limit(len(agency_ids) * 50)
        )
    ).scalars().all()
    for d in doc_rows:
        docs_by_agency.setdefault(d.agency_id, []).append(d)
        if len(docs_by_agency[d.agency_id]) >= 50:
            continue

    items = [
        {
            "agency_id": r.agency_id,
            "agency_name": name_by_id.get(r.agency_id, "Unknown"),
            "missing_count": int(r.missing_count),
            "worst_status": DocumentStatus.MISSING,
            "documents": docs_by_agency.get(r.agency_id, []),
        }
        for r in rows
    ]
    return items, total


# --------------------------------------------------------------------------
# Licenses
# --------------------------------------------------------------------------
async def list_licenses(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
    agency_id: uuid.UUID | None = None,
    status_filter: LicenseStatus | None = None,
    search: str | None = None,
    expiring_within_days: int | None = None,
    include_deleted: bool = False,
) -> tuple[list[AgencyLicense], int]:
    """Return a page of licenses and the total count."""
    filters = []
    if not include_deleted:
        filters.append(AgencyLicense.deleted_at.is_(None))
    if agency_id is not None:
        filters.append(AgencyLicense.agency_id == agency_id)
    if status_filter is not None:
        filters.append(AgencyLicense.status == status_filter)
    if expiring_within_days is not None:
        cutoff = datetime.now(tz=UTC) + timedelta(days=expiring_within_days)
        filters.append(AgencyLicense.expires_at <= cutoff)
    if search:
        pattern = f"%{search.strip()}%"
        filters.append(
            or_(
                AgencyLicense.name.ilike(pattern),
                AgencyLicense.reference_number.ilike(pattern),
                AgencyLicense.notes.ilike(pattern),
            )
        )

    count_stmt = select(func.count()).select_from(AgencyLicense)
    if filters:
        count_stmt = count_stmt.where(and_(*filters))
    total = (await session.execute(count_stmt)).scalar_one()

    list_stmt = (
        select(AgencyLicense)
        .order_by(AgencyLicense.expires_at.asc(), AgencyLicense.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    if filters:
        list_stmt = list_stmt.where(and_(*filters))

    rows = (await session.execute(list_stmt)).scalars().all()
    return list(rows), total


async def get_license(
    session: AsyncSession,
    *,
    license_id: uuid.UUID,
    include_deleted: bool = False,
) -> AgencyLicense:
    filters = [AgencyLicense.id == license_id]
    if not include_deleted:
        filters.append(AgencyLicense.deleted_at.is_(None))
    row = (
        await session.execute(select(AgencyLicense).where(*filters))
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(
            message="License not found.",
            details={"license_id": str(license_id), "resource": "agency_license"},
        )
    return row


async def create_license(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    name: str,
    expires_at: datetime,
    doc_type: DocumentType = DocumentType.LICENSE,
    status: LicenseStatus | None = None,
    issued_at: datetime | None = None,
    reference_number: str | None = None,
    notes: str | None = None,
) -> AgencyLicense:
    await _assert_agency_exists(session, agency_id)
    derived = status if status is not None else compute_license_status(expires_at)
    row = AgencyLicense(
        agency_id=agency_id,
        name=name,
        doc_type=doc_type,
        status=derived,
        issued_at=issued_at,
        expires_at=expires_at,
        reference_number=reference_number,
        notes=notes,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def update_license(
    session: AsyncSession,
    *,
    license_id: uuid.UUID,
    changes: dict[str, Any],
) -> AgencyLicense:
    row = await get_license(session, license_id=license_id, include_deleted=True)
    for key, value in changes.items():
        if hasattr(row, key):
            setattr(row, key, value)
    # Always recompute status when expires_at changes. If the caller
    # supplied an explicit status, respect it.
    if "expires_at" in changes and "status" not in changes:
        row.status = compute_license_status(row.expires_at)
    await session.commit()
    await session.refresh(row)
    return row


async def soft_delete_license(
    session: AsyncSession, *, license_id: uuid.UUID
) -> None:
    row = await get_license(session, license_id=license_id, include_deleted=True)
    if row.deleted_at is not None:
        return
    row.deleted_at = datetime.now(tz=UTC)
    await session.commit()


# --------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------
async def get_compliance_stats(session: AsyncSession) -> dict[str, int]:
    """Return the dashboard summary counts in one round-trip."""
    doc_total = (
        await session.execute(
            select(func.count())
            .select_from(AgencyDocument)
            .where(AgencyDocument.deleted_at.is_(None))
        )
    ).scalar_one()
    doc_buckets = (
        await session.execute(
            select(AgencyDocument.status, func.count())
            .where(AgencyDocument.deleted_at.is_(None))
            .group_by(AgencyDocument.status)
        )
    ).all()

    lic_total = (
        await session.execute(
            select(func.count())
            .select_from(AgencyLicense)
            .where(AgencyLicense.deleted_at.is_(None))
        )
    ).scalar_one()
    lic_buckets = (
        await session.execute(
            select(AgencyLicense.status, func.count())
            .where(AgencyLicense.deleted_at.is_(None))
            .group_by(AgencyLicense.status)
        )
    ).all()

    doc_by_status = {status: int(c) for status, c in doc_buckets}
    lic_by_status = {status: int(c) for status, c in lic_buckets}

    return {
        "documents_total": int(doc_total),
        "documents_missing": doc_by_status.get(DocumentStatus.MISSING, 0),
        "documents_expiring": doc_by_status.get(DocumentStatus.EXPIRING, 0),
        "documents_expired": doc_by_status.get(DocumentStatus.EXPIRED, 0),
        "documents_valid": doc_by_status.get(DocumentStatus.VALID, 0),
        "licenses_total": int(lic_total),
        "licenses_critical": lic_by_status.get(LicenseStatus.CRITICAL, 0),
        "licenses_warning": lic_by_status.get(LicenseStatus.WARNING, 0),
        "licenses_upcoming": lic_by_status.get(LicenseStatus.UPCOMING, 0),
        "licenses_valid": lic_by_status.get(LicenseStatus.VALID, 0),
        "licenses_expired": lic_by_status.get(LicenseStatus.EXPIRED, 0),
    }


__all__ = [
    "compute_document_status",
    "compute_license_status",
    "create_document",
    "create_license",
    "get_compliance_stats",
    "get_document",
    "get_license",
    "list_documents",
    "list_licenses",
    "list_missing_documents",
    "soft_delete_document",
    "soft_delete_license",
    "update_document",
    "update_license",
]