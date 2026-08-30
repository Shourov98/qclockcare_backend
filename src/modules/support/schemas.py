"""Pydantic schemas for the public support-ticket surface.

Conventions (matching `src/modules/tickets/schemas.py`):
  - Requests use `extra="forbid"` so a typo in a field name is a
    hard 422 rather than a silently-ignored field.
  - All text fields strip whitespace and reject empty/whitespace-
    only strings via a shared `_strip_required_text` validator.
  - Responses use `from_attributes=True` so ORM rows pass straight
    into `model_validate`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.shared.domain.enums import (
    SupportTicketAuthorKind,
    SupportTicketPriority,
    SupportTicketStatus,
)


# --------------------------------------------------------------------------
# Shared validators
# --------------------------------------------------------------------------
def _strip_required_text(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be empty or whitespace-only")
    return stripped


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------
class SupportTicketCreateRequest(BaseModel):
    """Body for `POST /portal/support/tickets` (PATIENT or GUARDIAN).

    `body` becomes the first message in the thread. `patient_id` is
    optional for PATIENT callers (they may be reporting on their own
    account, e.g. "I can't log in") and **required** for GUARDIAN
    callers — the service layer enforces the active-linkage check.
    """

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1)
    priority: SupportTicketPriority = SupportTicketPriority.MEDIUM
    patient_id: uuid.UUID | None = None

    @field_validator("subject", "body")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        return _strip_required_text(value)


class SupportTicketReplyRequest(BaseModel):
    """Body for `POST /portal/support/tickets/{id}/messages` and
    `POST /agency/support/tickets/{id}/messages`."""

    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1)

    @field_validator("body")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        return _strip_required_text(value)


class SupportTicketStatusRequest(BaseModel):
    """Body for `PATCH /agency/support/tickets/{id}/status`.

    AGENCY_ADMIN-only endpoint. Both fields are technically required,
    but `priority` is optional — you can change just status (the more
    common case) or both at once.
    """

    model_config = ConfigDict(extra="forbid")

    status: SupportTicketStatus
    priority: SupportTicketPriority | None = None


# --------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------
class SupportTicketMessageResponse(BaseModel):
    """One thread message."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ticket_id: uuid.UUID
    author_user_id: uuid.UUID
    author_kind: SupportTicketAuthorKind
    # Display label — computed in the service layer from the joined
    # `User.full_name`. We accept it as a free-form field here so the
    # Pydantic model can be validated from either an ORM row or a dict.
    author_display_name: str | None = None
    body: str
    created_at: datetime


class SupportTicketResponse(BaseModel):
    """List-item / summary shape. No nested `messages` array.

    `last_message_preview` is the first ~120 chars of the most recent
    message body (used for the inbox card preview).
    `message_count` is the total number of messages in the thread.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agency_id: uuid.UUID
    patient_id: uuid.UUID | None = None
    patient_name: str | None = None
    patient_initials: str | None = None
    reporter_user_id: uuid.UUID
    reporter_display_name: str | None = None
    reporter_kind: SupportTicketAuthorKind
    subject: str
    status: SupportTicketStatus
    priority: SupportTicketPriority
    last_message_at: datetime | None = None
    last_message_preview: str | None = None
    resolved_at: datetime | None = None
    closed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    message_count: int = 0


class SupportTicketDetailResponse(SupportTicketResponse):
    """Detail shape — same as the summary plus the full message thread."""

    messages: list[SupportTicketMessageResponse] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------
class SupportTicketListPagination(BaseModel):
    page: int
    page_size: int
    total: int
    total_pages: int


class SupportTicketListResponse(BaseModel):
    data: list[SupportTicketResponse]
    pagination: SupportTicketListPagination


__all__ = [
    "SupportTicketCreateRequest",
    "SupportTicketDetailResponse",
    "SupportTicketListPagination",
    "SupportTicketListResponse",
    "SupportTicketMessageResponse",
    "SupportTicketReplyRequest",
    "SupportTicketResponse",
    "SupportTicketStatusRequest",
]