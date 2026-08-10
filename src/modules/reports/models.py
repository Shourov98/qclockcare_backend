"""Reports module — ORM models.

Owns one table: `report_runs`. Each row is one Claude narrative generation:
the params the user passed, the data snapshot we sent to Claude, the
text that came back, the token count, and the run status. The aggregate
snapshot is persisted so PDF/CSV/XLSX exports don't re-query the DB
when the user clicks "Export" minutes after the narrative rendered.

The narrative itself is written incrementally — the row is created
with `status='streaming'` before Claude is called, then UPDATEd as
tokens arrive, then finalized as `status='completed'` (or `failed`)
once the SDK returns the `message_stop` event (or raises).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.domain.base_entity import Base, IdMixin


class ReportRun(IdMixin, Base):
    """One Claude narrative generation.

    `params` is the user-supplied filter set (date range, columns,
    program type, etc.) — the same dict the SPA sent in the request body.
    `aggregate_payload` is what the backend actually shipped to Claude;
    persisting it lets exports skip a re-aggregation. `narrative` is the
    final streamed text; while streaming, it grows via UPDATE.

    Status walk: `streaming` (row inserted, awaiting first token) →
    `completed` (Claude finished, full narrative persisted, token counts
    populated) or `failed` (SDK raised, error captured, no narrative).
    """

    __tablename__ = "report_runs"

    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id", ondelete="CASCADE"),
        nullable=False,
    )
    requested_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # The 9-card ReportType enum, stored as its string value for
    # human-readable debugging in the Supabase dashboard. We don't
    # create a Postgres ENUM here — adding a new report type would
    # then require a migration to ALTER TYPE, which is overkill for
    # a table that grows linearly with new feature cards.
    report_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # User-supplied filters (date_from / date_to / program_type / columns).
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # The data snapshot we sent to Claude. Persisted so exports
    # (PDF/CSV/XLSX) reuse it without re-querying.
    aggregate_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    # The streamed narrative. NULL while streaming (we don't want to
    # rewrite a Text column on every token — see `service.py` for the
    # buffer-and-flush pattern). Populated on completion.
    narrative: Mapped[str | None] = mapped_column(Text, nullable=True)
    # `streaming` while waiting for tokens, `completed` once Claude's
    # `message_stop` event arrives, `failed` on SDK exception. No
    # UPDATE/DELETE trigger — the run history is mutable so we can
    # patch rows in-flight.
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default="streaming",
    )
    claude_model: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=""
    )
    input_tokens: Mapped[int] = mapped_column(
        # Use 0 (not NULL) so reports can SUM() without coalesce.
        nullable=False,
        server_default=text("0"),
    )
    output_tokens: Mapped[int] = mapped_column(
        nullable=False,
        server_default=text("0"),
    )
    # USD cost = (input_tokens * claude_input_price) + (output_tokens * claude_output_price)
    # for the model in use. Stored at 6 decimal places — Anthropic bills
    # in fractions of a cent. NULL while streaming; backfilled on completion.
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    # Populated only on `status='failed'` so users can see why a report
    # blew up without having to check the server logs.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    # Set on completion (status transitions to completed|failed). NULL
    # while still streaming so a stuck run is visible.
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # Primary list path: agency's report history sidebar.
        Index(
            "idx_report_runs_agency_created",
            "agency_id",
            text("created_at DESC"),
        ),
        # Per-user history (for "reports I generated" filter).
        Index(
            "idx_report_runs_user_created",
            "requested_by_user_id",
            text("created_at DESC"),
        ),
        # Filter by report type when the user clicks a specific card
        # to see all past runs of that kind.
        Index(
            "idx_report_runs_agency_type_created",
            "agency_id",
            "report_type",
            text("created_at DESC"),
        ),
        CheckConstraint(
            "status IN ('streaming', 'completed', 'failed')",
            name="ck_report_runs_status",
        ),
        CheckConstraint(
            "report_type <> ''",
            name="ck_report_runs_report_type_non_empty",
        ),
    )


__all__ = ["ReportRun"]
