"""Compliance issue queue — model + schemas + service.

Lives in `compliance/issues.py` rather than a sibling module because the
admin compliance surface is one feature (documents + licenses + issues)
under `/admin/compliance`. The router (`compliance/router.py`) imports
the service functions defined here.

Endpoints served (mounted by `compliance/router.py`):
  GET    /admin/compliance/issues
  GET    /admin/compliance/issues/stats
  POST   /admin/compliance/issues
  GET    /admin/compliance/issues/{id}
  PATCH  /admin/compliance/issues/{id}
  POST   /admin/compliance/issues/{id}/resolve
  POST   /admin/compliance/issues/{id}/dismiss
  POST   /admin/compliance/issues/{id}/assign
  DELETE /admin/compliance/issues/{id}

Auth gate: `require_scope(AdminScope.AGENCIES)` — same as the existing
documents/licenses surface. Soft-delete via `deleted_at`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Enum as SAEnum, String, and_, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from src.core.exceptions import NotFoundError
from src.shared.domain.base_entity import (
    Base,
    IdMixin,
    SoftDeleteMixin,
    TimestampedMixin,
)
from src.shared.domain.enum_mapping import pg_name
from src.shared.domain.enums import (
    ComplianceIssueCategory,
    ComplianceIssueSeverity,
    ComplianceIssueStatus,
)


class ComplianceIssue(
    IdMixin, TimestampedMixin, SoftDeleteMixin, Base
):
    """A single row in the admin compliance issue queue.

    The FE renders these in `ComplianceIssueQueueTable.tsx` with a
    severity badge, title + description, agency, assignee, due date,
    and a status pill. Status moves through:
        OPEN → IN_PROGRESS → PENDING_REVIEW → RESOLVED | DISMISSED.

    `extra` is reserved for forward-compat (file_url attachments, audit
    trail breadcrumbs, etc.) and should not be used for required fields.
    """

    __tablename__ = "compliance_issues"

    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    description: Mapped[str | None] = mapped_column(nullable=True)

    severity: Mapped[ComplianceIssueSeverity] = mapped_column(
        SAEnum(ComplianceIssueSeverity, name=pg_name(ComplianceIssueSeverity)),
        nullable=False,
        default=ComplianceIssueSeverity.MEDIUM,
        server_default=ComplianceIssueSeverity.MEDIUM.value,
        index=True,
    )
    status: Mapped[ComplianceIssueStatus] = mapped_column(
        SAEnum(ComplianceIssueStatus, name=pg_name(ComplianceIssueStatus)),
        nullable=False,
        default=ComplianceIssueStatus.OPEN,
        server_default=ComplianceIssueStatus.OPEN.value,
        index=True,
    )
    category: Mapped[ComplianceIssueCategory] = mapped_column(
        SAEnum(ComplianceIssueCategory, name=pg_name(ComplianceIssueCategory)),
        nullable=False,
        default=ComplianceIssueCategory.OTHER,
        server_default=ComplianceIssueCategory.OTHER.value,
    )

    reporter_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    assignee_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    due_at: Mapped[datetime | None] = mapped_column(nullable=True, index=True)

    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)
    resolved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )

    # Forward-compat pointer to the record that surfaced this issue
    # (e.g. an expired `AgencyLicense.id`, an overdue `AgencyDocument.id`,
    # a `Visit.id` with a missing signature, etc.).
    linked_entity_type: Mapped[str | None] = mapped_column(nullable=True)
    linked_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    extra: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )


# --------------------------------------------------------------------------
# Pydantic schemas
# --------------------------------------------------------------------------
class ComplianceIssueCreateRequest(BaseModel):
    """Create a compliance issue row."""

    model_config = ConfigDict(extra="forbid")

    agency_id: uuid.UUID
    title: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4000)
    severity: ComplianceIssueSeverity = ComplianceIssueSeverity.MEDIUM
    category: ComplianceIssueCategory = ComplianceIssueCategory.OTHER
    assignee_user_id: uuid.UUID | None = None
    due_at: datetime | None = None
    linked_entity_type: str | None = Field(default=None, max_length=64)
    linked_entity_id: uuid.UUID | None = None

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty or whitespace-only")
        return stripped


class ComplianceIssueUpdateRequest(BaseModel):
    """Partial update of an issue (admin triage)."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4000)
    severity: ComplianceIssueSeverity | None = None
    category: ComplianceIssueCategory | None = None
    assignee_user_id: uuid.UUID | None = None
    due_at: datetime | None = None
    linked_entity_type: str | None = Field(default=None, max_length=64)
    linked_entity_id: uuid.UUID | None = None

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty or whitespace-only")
        return stripped


class ComplianceIssueResolveRequest(BaseModel):
    """`POST /admin/compliance/issues/{id}/resolve` payload."""

    model_config = ConfigDict(extra="forbid")

    resolution_note: str | None = Field(default=None, max_length=4000)


class ComplianceIssueDismissRequest(BaseModel):
    """`POST /admin/compliance/issues/{id}/dismiss` payload."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=4000)


class ComplianceIssueAssignRequest(BaseModel):
    """`POST /admin/compliance/issues/{id}/assign` payload."""

    model_config = ConfigDict(extra="forbid")

    assignee_user_id: uuid.UUID


class ComplianceIssueResponse(BaseModel):
    """One compliance issue."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agency_id: uuid.UUID
    title: str
    description: str | None
    severity: ComplianceIssueSeverity
    status: ComplianceIssueStatus
    category: ComplianceIssueCategory
    reporter_user_id: uuid.UUID | None
    assignee_user_id: uuid.UUID | None
    due_at: datetime | None
    resolved_at: datetime | None
    resolved_by_user_id: uuid.UUID | None
    linked_entity_type: str | None
    linked_entity_id: uuid.UUID | None
    extra: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class ComplianceIssueListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: list[ComplianceIssueResponse]
    pagination: dict[str, int]


class ComplianceIssueStatsResponse(BaseModel):
    """Dashboard summary counts for the issue queue widget."""

    model_config = ConfigDict(extra="forbid")

    total: int
    open: int
    in_progress: int
    pending_review: int
    resolved: int
    dismissed: int

    # Severity breakdown — drives the FE colour-coded stats row.
    critical: int
    high: int
    medium: int
    low: int

    # Convenience counters used by the dashboard header.
    overdue: int
    due_within_7_days: int


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _build_pagination(
    *, page: int, page_size: int, total: int
) -> dict[str, int]:
    total_pages = (total + page_size - 1) // page_size if total else 0
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
    }


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------
async def list_compliance_issues(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
    agency_id: uuid.UUID | None = None,
    severity: ComplianceIssueSeverity | None = None,
    status_filter: ComplianceIssueStatus | None = None,
    category: ComplianceIssueCategory | None = None,
    assignee_user_id: uuid.UUID | None = None,
    search: str | None = None,
    include_resolved: bool = True,
) -> tuple[list[ComplianceIssue], int]:
    """Return a page of compliance issues and the total count.

    The `status_filter` aliases the `status` enum (same shape as the FE's
    filter dropdown). `include_resolved=False` hides RESOLVED + DISMISSED
    rows so the queue widget can show "Open issues only".
    """
    filters = [ComplianceIssue.deleted_at.is_(None)]
    if agency_id is not None:
        filters.append(ComplianceIssue.agency_id == agency_id)
    if severity is not None:
        filters.append(ComplianceIssue.severity == severity)
    if status_filter is not None:
        filters.append(ComplianceIssue.status == status_filter)
    if category is not None:
        filters.append(ComplianceIssue.category == category)
    if assignee_user_id is not None:
        filters.append(ComplianceIssue.assignee_user_id == assignee_user_id)
    if not include_resolved:
        filters.append(
            ComplianceIssue.status.notin_(
                [
                    ComplianceIssueStatus.RESOLVED,
                    ComplianceIssueStatus.DISMISSED,
                ]
            )
        )
    if search:
        pattern = f"%{search.strip()}%"
        filters.append(
            or_(
                ComplianceIssue.title.ilike(pattern),
                ComplianceIssue.description.ilike(pattern),
            )
        )

    count_stmt = select(func.count()).select_from(ComplianceIssue)
    if filters:
        count_stmt = count_stmt.where(and_(*filters))
    total = (await session.execute(count_stmt)).scalar_one()

    list_stmt = (
        select(ComplianceIssue)
        .order_by(ComplianceIssue.created_at.desc(), ComplianceIssue.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    if filters:
        list_stmt = list_stmt.where(and_(*filters))

    rows = (await session.execute(list_stmt)).scalars().all()
    return list(rows), total


async def get_compliance_issue(
    session: AsyncSession,
    *,
    issue_id: uuid.UUID,
    include_deleted: bool = False,
) -> ComplianceIssue:
    filters = [ComplianceIssue.id == issue_id]
    if not include_deleted:
        filters.append(ComplianceIssue.deleted_at.is_(None))
    row = (
        await session.execute(select(ComplianceIssue).where(*filters))
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(
            message="Compliance issue not found.",
            details={"issue_id": str(issue_id), "resource": "compliance_issue"},
        )
    return row


async def create_compliance_issue(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    title: str,
    severity: ComplianceIssueSeverity = ComplianceIssueSeverity.MEDIUM,
    category: ComplianceIssueCategory = ComplianceIssueCategory.OTHER,
    description: str | None = None,
    reporter_user_id: uuid.UUID | None = None,
    assignee_user_id: uuid.UUID | None = None,
    due_at: datetime | None = None,
    linked_entity_type: str | None = None,
    linked_entity_id: uuid.UUID | None = None,
) -> ComplianceIssue:
    """Insert a new compliance issue row."""
    row = ComplianceIssue(
        agency_id=agency_id,
        title=title,
        description=description,
        severity=severity,
        category=category,
        reporter_user_id=reporter_user_id,
        assignee_user_id=assignee_user_id,
        due_at=due_at,
        linked_entity_type=linked_entity_type,
        linked_entity_id=linked_entity_id,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def update_compliance_issue(
    session: AsyncSession,
    *,
    issue_id: uuid.UUID,
    changes: dict[str, Any],
) -> ComplianceIssue:
    """Patch an issue. Whitelisted columns only."""
    row = await get_compliance_issue(
        session, issue_id=issue_id, include_deleted=True
    )
    # Prevent stomping on lifecycle columns that have their own endpoints.
    protected = {"id", "created_at", "updated_at", "deleted_at", "resolved_at",
                 "resolved_by_user_id", "reporter_user_id", "extra"}
    for key, value in changes.items():
        if key in protected:
            continue
        if hasattr(row, key):
            setattr(row, key, value)
    await session.commit()
    await session.refresh(row)
    return row


async def resolve_compliance_issue(
    session: AsyncSession,
    *,
    issue_id: uuid.UUID,
    resolved_by_user_id: uuid.UUID,
) -> ComplianceIssue:
    """Mark an issue as RESOLVED. Stamps `resolved_at` + `resolved_by_user_id`."""
    row = await get_compliance_issue(session, issue_id=issue_id)
    row.status = ComplianceIssueStatus.RESOLVED
    row.resolved_at = datetime.now(tz=UTC)
    row.resolved_by_user_id = resolved_by_user_id
    await session.commit()
    await session.refresh(row)
    return row


async def dismiss_compliance_issue(
    session: AsyncSession,
    *,
    issue_id: uuid.UUID,
    resolved_by_user_id: uuid.UUID,
) -> ComplianceIssue:
    """Mark an issue as DISMISSED. Stamps `resolved_at` + `resolved_by_user_id`."""
    row = await get_compliance_issue(session, issue_id=issue_id)
    row.status = ComplianceIssueStatus.DISMISSED
    row.resolved_at = datetime.now(tz=UTC)
    row.resolved_by_user_id = resolved_by_user_id
    await session.commit()
    await session.refresh(row)
    return row


async def assign_compliance_issue(
    session: AsyncSession,
    *,
    issue_id: uuid.UUID,
    assignee_user_id: uuid.UUID,
) -> ComplianceIssue:
    """Assign an issue to a user; auto-flips OPEN → IN_PROGRESS."""
    row = await get_compliance_issue(session, issue_id=issue_id)
    row.assignee_user_id = assignee_user_id
    if row.status == ComplianceIssueStatus.OPEN:
        row.status = ComplianceIssueStatus.IN_PROGRESS
    await session.commit()
    await session.refresh(row)
    return row


async def soft_delete_compliance_issue(
    session: AsyncSession, *, issue_id: uuid.UUID
) -> None:
    row = await get_compliance_issue(
        session, issue_id=issue_id, include_deleted=True
    )
    if row.deleted_at is not None:
        return
    row.deleted_at = datetime.now(tz=UTC)
    await session.commit()


async def get_compliance_issue_stats(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """Aggregate counts for the FE dashboard header.

    Buckets: by status, by severity, overdue, due-within-7-days.
    `overdue` = status in (OPEN, IN_PROGRESS, PENDING_REVIEW) AND
    due_at < now().
    """
    base_filters = [ComplianceIssue.deleted_at.is_(None)]
    if agency_id is not None:
        base_filters.append(ComplianceIssue.agency_id == agency_id)

    def _stmt(extra=None):
        s = select(func.count()).select_from(ComplianceIssue)
        f = list(base_filters)
        if extra:
            f.extend(extra)
        if f:
            s = s.where(and_(*f))
        return s

    now = datetime.now(tz=UTC)
    open_statuses = [
        ComplianceIssueStatus.OPEN,
        ComplianceIssueStatus.IN_PROGRESS,
        ComplianceIssueStatus.PENDING_REVIEW,
    ]

    total = (await session.execute(_stmt())).scalar_one()
    by_status_rows = (
        await session.execute(
            select(ComplianceIssue.status, func.count())
            .where(*base_filters)
            .group_by(ComplianceIssue.status)
        )
    ).all()
    by_severity_rows = (
        await session.execute(
            select(ComplianceIssue.severity, func.count())
            .where(*base_filters)
            .group_by(ComplianceIssue.severity)
        )
    ).all()
    status_map = {s: int(c) for s, c in by_status_rows}
    severity_map = {s: int(c) for s, c in by_severity_rows}
    overdue = (
        await session.execute(
            _stmt(
                [
                    ComplianceIssue.status.in_(open_statuses),
                    ComplianceIssue.due_at.is_not(None),
                    ComplianceIssue.due_at < now,
                ]
            )
        )
    ).scalar_one()
    due_within_7 = (
        await session.execute(
            _stmt(
                [
                    ComplianceIssue.status.in_(open_statuses),
                    ComplianceIssue.due_at.is_not(None),
                    ComplianceIssue.due_at >= now,
                    ComplianceIssue.due_at <= now + timedelta(days=7),
                ]
            )
        )
    ).scalar_one()

    return {
        "total": int(total),
        "open": status_map.get(ComplianceIssueStatus.OPEN, 0),
        "in_progress": status_map.get(ComplianceIssueStatus.IN_PROGRESS, 0),
        "pending_review": status_map.get(
            ComplianceIssueStatus.PENDING_REVIEW, 0
        ),
        "resolved": status_map.get(ComplianceIssueStatus.RESOLVED, 0),
        "dismissed": status_map.get(ComplianceIssueStatus.DISMISSED, 0),
        "critical": severity_map.get(ComplianceIssueSeverity.CRITICAL, 0),
        "high": severity_map.get(ComplianceIssueSeverity.HIGH, 0),
        "medium": severity_map.get(ComplianceIssueSeverity.MEDIUM, 0),
        "low": severity_map.get(ComplianceIssueSeverity.LOW, 0),
        "overdue": int(overdue),
        "due_within_7_days": int(due_within_7),
    }


# Re-export pagination helper for the router layer.
build_issue_pagination = _build_pagination


__all__ = [
    "ComplianceIssue",
    "ComplianceIssueAssignRequest",
    "ComplianceIssueCreateRequest",
    "ComplianceIssueDismissRequest",
    "ComplianceIssueListResponse",
    "ComplianceIssueResolveRequest",
    "ComplianceIssueResponse",
    "ComplianceIssueStatsResponse",
    "ComplianceIssueUpdateRequest",
    "assign_compliance_issue",
    "create_compliance_issue",
    "dismiss_compliance_issue",
    "get_compliance_issue",
    "get_compliance_issue_stats",
    "list_compliance_issues",
    "resolve_compliance_issue",
    "soft_delete_compliance_issue",
    "update_compliance_issue",
]