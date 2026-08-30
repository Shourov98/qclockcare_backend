"""ORM models for the public help & support ticket surface.

Tables:
- `support_tickets`         — one row per help ticket opened by a patient
                              or guardian against their agency's admin.
- `support_ticket_messages` — threaded messages on a ticket (the
                              "first message" is the ticket body;
                              subsequent rows are replies).

Distinct from `src/modules/tickets/models.py` which holds the
internal-admin / platform-team helpdesk (`/admin/tickets`).

`support_tickets` is tenant-scoped via `agency_id` (NOT NULL +
FK CASCADE on delete). RLS policies on the table scope reads by
`current_setting('app.current_agency_id')`; the service layer also
verifies patient/guardian linkage before exposing a ticket to a
non-admin caller, so cross-agency reads fail with 404 (not 403)
to match the portal convention.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.domain.base_entity import Base, IdMixin, TimestampedMixin
from src.shared.domain.enum_mapping import pg_name
from src.shared.domain.enums import (
    SupportTicketAuthorKind,
    SupportTicketPriority,
    SupportTicketStatus,
)

if TYPE_CHECKING:
    from src.modules.agencies.models import Agency
    from src.modules.identity.models import User
    from src.modules.patients.models import PatientProfile


class SupportTicket(IdMixin, TimestampedMixin, Base):
    """A help/support ticket opened by a patient or guardian.

    `subject` is the human-readable headline (e.g. "Caregiver was 30
    minutes late"). The full opening message is stored as the first
    `SupportTicketMessage` row with `created_at == ticket.created_at`,
    which keeps the thread layout symmetric — list/get responses
    don't have to special-case "first message vs reply".

    `last_message_at` + `last_message_by_user_id` are denormalised
    counters the inbox view needs for sort + presence-style "you have
    a new reply" badges. The service layer keeps them in sync on
    every reply insert.
    """

    __tablename__ = "support_tickets"

    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc=(
            "NULL for patient-self tickets about their own account "
            "(e.g. 'I can't log in'). REQUIRED for GUARDIAN reporters — "
            "the service layer enforces the active-linkage check."
        ),
    )
    reporter_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    reporter_kind: Mapped[SupportTicketAuthorKind] = mapped_column(
        Enum(
            SupportTicketAuthorKind,
            name=pg_name(SupportTicketAuthorKind),
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
        doc="Which side opened the ticket — denormalised off reporter role.",
    )

    subject: Mapped[str] = mapped_column(String(255), nullable=False)

    status: Mapped[SupportTicketStatus] = mapped_column(
        Enum(
            SupportTicketStatus,
            name=pg_name(SupportTicketStatus),
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
        default=SupportTicketStatus.OPEN,
        server_default=SupportTicketStatus.OPEN.value,
        index=True,
    )
    priority: Mapped[SupportTicketPriority] = mapped_column(
        Enum(
            SupportTicketPriority,
            name=pg_name(SupportTicketPriority),
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
        default=SupportTicketPriority.MEDIUM,
        server_default=SupportTicketPriority.MEDIUM.value,
        index=True,
    )

    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_message_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="Stamped when status transitions to RESOLVED.",
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="Stamped when status transitions to CLOSED (terminal).",
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="Soft-delete — service layer filters these out by default.",
    )

    messages: Mapped[list["SupportTicketMessage"]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
        order_by="SupportTicketMessage.created_at",
    )

    if TYPE_CHECKING:  # pragma: no cover - typing only
        # Forward declarations for IDE/type-checker only. The actual
        # relationships live on the foreign-side models.
        _agency: Agency | None
        _patient: PatientProfile | None
        _reporter: User | None

    __table_args__ = (
        Index(
            "idx_support_tickets_agency_status_last_msg",
            "agency_id",
            "status",
            "last_message_at",
        ),
        Index(
            "idx_support_tickets_reporter_created",
            "reporter_user_id",
            "created_at",
        ),
    )


class SupportTicketMessage(IdMixin, TimestampedMixin, Base):
    """One message in a support ticket thread.

    The very first message in a thread (the ticket body) is stored
    here too — same shape as every subsequent reply. That keeps the
    thread rendering symmetric and avoids a "first message is special"
    branch in the FE.
    """

    __tablename__ = "support_ticket_messages"

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("support_tickets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    author_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    author_kind: Mapped[SupportTicketAuthorKind] = mapped_column(
        Enum(
            SupportTicketAuthorKind,
            name=pg_name(SupportTicketAuthorKind),
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
    )

    body: Mapped[str] = mapped_column(Text, nullable=False)

    ticket: Mapped["SupportTicket"] = relationship(back_populates="messages")

    __table_args__ = (
        Index(
            "idx_support_ticket_messages_ticket_created",
            "ticket_id",
            "created_at",
        ),
    )


__all__ = ["SupportTicket", "SupportTicketMessage"]