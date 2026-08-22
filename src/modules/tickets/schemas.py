"""Tickets Pydantic schemas — request / response DTOs.

Mirrors `tickets.models` and the routes documented in `tickets/__init__.py`.
All schemas use `extra="forbid"` so unknown fields produce a 422.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from src.shared.domain.enums import TicketPriority, TicketStatus
from src.modules.tickets.models import TicketCommentKind


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------
class TicketCreateRequest(BaseModel):
    """Create a new support ticket.

    `agency_id` is optional — leave it `None` for cross-tenant issues
    (e.g. "Stripe webhooks are dropping globally").
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "title": "Stripe webhook deliveries dropped at 14:32 UTC",
                "description": "Customer portal stopped receiving invoice.paid events.",
                "priority": "HIGH",
                "agency_id": None,
                "assignee_user_id": None,
            }
        },
    )

    title: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1)
    priority: TicketPriority = TicketPriority.MEDIUM
    agency_id: uuid.UUID | None = None
    assignee_user_id: uuid.UUID | None = None

    @field_validator("title", "description")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty or whitespace-only")
        return stripped


class TicketUpdateRequest(BaseModel):
    """Partial update — only fields the caller supplies are touched."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(default=None, min_length=1)
    status: TicketStatus | None = None
    priority: TicketPriority | None = None
    agency_id: uuid.UUID | None = None
    assignee_user_id: uuid.UUID | None = None

    @field_validator("title", "description")
    @classmethod
    def _strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty or whitespace-only")
        return stripped


class TicketCommentCreateRequest(BaseModel):
    """Append a comment or system event to a ticket."""

    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1)
    kind: TicketCommentKind = TicketCommentKind.COMMENT
    event_metadata: dict = Field(default_factory=dict)

    @field_validator("body")
    @classmethod
    def _strip_body(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty or whitespace-only")
        return stripped


# --------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------
class TicketCommentAuthorResponse(BaseModel):
    """Lightweight author projection for timeline rendering."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str
    email: EmailStr


class TicketCommentResponse(BaseModel):
    """One timeline entry on a ticket."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ticket_id: uuid.UUID
    author_user_id: uuid.UUID
    author: TicketCommentAuthorResponse | None = None
    kind: TicketCommentKind
    body: str
    event_metadata: dict
    edited_at: datetime | None
    created_at: datetime
    updated_at: datetime


class TicketResponse(BaseModel):
    """One ticket, with its comments eagerly loaded."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    title: str
    description: str
    status: TicketStatus
    priority: TicketPriority
    agency_id: uuid.UUID | None
    reporter_user_id: uuid.UUID
    assignee_user_id: uuid.UUID | None
    deleted_at: datetime | None
    extra: dict
    created_at: datetime
    updated_at: datetime
    comments: list[TicketCommentResponse] = Field(default_factory=list)


class TicketListItemResponse(BaseModel):
    """Slim ticket row for the list view (no comments)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    title: str
    status: TicketStatus
    priority: TicketPriority
    agency_id: uuid.UUID | None
    reporter_user_id: uuid.UUID
    assignee_user_id: uuid.UUID | None
    deleted_at: datetime | None
    created_at: datetime
    updated_at: datetime
    # `attachment_count` is derived from comments.kind = ATTACHMENT
    # in the service layer — kept as a sibling column so the FE
    # doesn't need a second roundtrip.
    attachment_count: int = 0


class TicketListResponse(BaseModel):
    """Paginated envelope around `TicketListItemResponse`."""

    model_config = ConfigDict(extra="forbid")

    data: list[TicketListItemResponse]
    pagination: "TicketListPagination"


class TicketListPagination(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int
    page_size: int
    total: int
    total_pages: int


class TicketStatusCounts(BaseModel):
    """Per-status counts for the dashboard summary cards."""

    model_config = ConfigDict(extra="forbid")

    OPEN: int = 0
    IN_PROGRESS: int = 0
    PENDING: int = 0
    RESOLVED: int = 0
    CLOSED: int = 0


class TicketStatsResponse(BaseModel):
    """Aggregate counts for the dashboard summary cards."""

    model_config = ConfigDict(extra="forbid")

    total: int
    by_status: TicketStatusCounts
    by_priority: dict[TicketPriority, int]


__all__ = [
    "TicketCommentAuthorResponse",
    "TicketCommentCreateRequest",
    "TicketCommentResponse",
    "TicketCreateRequest",
    "TicketListItemResponse",
    "TicketListPagination",
    "TicketListResponse",
    "TicketResponse",
    "TicketStatsResponse",
    "TicketStatusCounts",
    "TicketUpdateRequest",
]
