"""Reports module — request/response DTOs.

Public API surface for the `/reports/*` endpoints. Mirrors the 9 cards
on the Reports dashboard plus an `AI_INSIGHTS` pseudo-type that drives
the AI Insights banner. The backend also exposes run history (list /
get) and exports (PDF / CSV / XLSX) — those DTOs live here too.

`ReportRunParams` is intentionally loose — every filter key is
optional and the report-type-specific ones are no-ops for the types
that don't recognize them (e.g. `program_type` only matters for
visits / staff / compliance). This keeps the route signature stable
across the 9 cards without a per-type schema explosion.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# --------------------------------------------------------------------------
# ReportType — the 9 dashboard cards + 1 AI pseudo-type
# --------------------------------------------------------------------------
class ReportType(StrEnum):
    """One card on the Reports dashboard, matched by enum value.

    String-valued so the frontend can compare the card's `id` against
    `ReportType.value` without an extra mapping table. The enum order
    matches the dashboard grid order (see `reportsGrid` in
    `farhan-salad-website/app/(dashboard)/reports/page.tsx`).
    """

    VISIT_SUMMARY = "visit_summary"
    BILLING = "billing"
    COMPLIANCE = "compliance"
    CLIENT = "client"
    STAFF = "staff"
    EVV = "evv"
    GROUP_HOME = "group_home"
    AUDIT_READINESS = "audit_readiness"
    CUSTOM = "custom"
    # Synthesis across all reports — what the AI Insights banner asks for.
    AI_INSIGHTS = "ai_insights"


# Format for the export endpoint. `xlsx` is the on-the-wire MIME that
# Excel expects; we accept both `xlsx` and `excel` for ergonomics.
ExportFormat = Literal["pdf", "csv", "xlsx"]


# --------------------------------------------------------------------------
# Request bodies
# --------------------------------------------------------------------------
class ReportRunParams(BaseModel):
    """User-supplied filters — sent verbatim into the Claude prompt.

    All fields are optional; the backend defaults to "last 30 days"
    if both `date_from` and `date_to` are missing. The frontend's
    "Builder" form passes this as the JSON body of the SSE request.
    """

    model_config = ConfigDict(extra="allow")

    date_from: date | None = Field(
        default=None,
        description="ISO date. Defaults to 30 days ago.",
    )
    date_to: date | None = Field(
        default=None,
        description="ISO date. Defaults to today.",
    )
    program_type: str | None = Field(
        default=None,
        description="Optional program filter (ARMHS, PCA, …).",
    )
    caregiver_id: uuid.UUID | None = Field(
        default=None,
        description="Optional staff filter for client/staff reports.",
    )
    client_id: uuid.UUID | None = Field(
        default=None,
        description="Optional patient filter for client/visit reports.",
    )
    # For the CUSTOM report type — which columns to include in the
    # export. Unknown columns are dropped by the aggregator with a
    # warning rather than raising. Other report types ignore this.
    columns: list[str] | None = Field(
        default=None,
        description="Custom-report column whitelist.",
    )


class ReportRunCreate(BaseModel):
    """Body for `POST /reports/{type}/stream`."""

    params: ReportRunParams = Field(
        default_factory=ReportRunParams,
        description="Filter set — passed to the aggregator and the prompt.",
    )


# --------------------------------------------------------------------------
# Response / read models
# --------------------------------------------------------------------------
class ReportRunSummary(BaseModel):
    """One row in the report-history sidebar."""

    id: uuid.UUID
    report_type: ReportType
    status: Literal["streaming", "completed", "failed"]
    created_at: datetime
    completed_at: datetime | None
    narrative_chars: int = Field(
        description="Length of the narrative so far (0 while streaming).",
    )


class ReportRunRead(BaseModel):
    """Full report run — read via `GET /reports/runs/{id}`."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agency_id: uuid.UUID
    requested_by_user_id: uuid.UUID | None
    report_type: ReportType
    params: dict[str, Any]
    aggregate_payload: dict[str, Any] | None
    narrative: str | None
    status: Literal["streaming", "completed", "failed"]
    claude_model: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal | None
    error: str | None
    created_at: datetime
    completed_at: datetime | None


# --------------------------------------------------------------------------
# Streaming SSE event shape
# --------------------------------------------------------------------------
class StreamEvent(BaseModel):
    """One SSE frame sent over the `/reports/{type}/stream` connection.

    Format on the wire: `data: <json>\n\n`. The frontend parses each
    frame's JSON and dispatches on `kind`. The whole stream is one
    `text/event-stream` response with `media_type="text/event-stream"`
    — the browser's `EventSource` reads frames until the connection
    closes.
    """

    kind: Literal["delta", "run_meta", "final", "error"]
    delta: str | None = Field(
        default=None,
        description="Text chunk for `kind='delta'`. Concatenate to render.",
    )
    run_id: uuid.UUID | None = Field(
        default=None,
        description="Set in the first `run_meta` frame so the SPA can store the id.",
    )
    total_tokens: int | None = Field(
        default=None,
        description="Set in the `final` frame — input + output sum.",
    )
    cost_usd: Decimal | None = Field(
        default=None,
        description="Set in the `final` frame so the UI can show cost.",
    )
    error: str | None = Field(
        default=None,
        description="Set in `kind='error'` frames; never raised to the client.",
    )


__all__ = [
    "ExportFormat",
    "ReportRunCreate",
    "ReportRunParams",
    "ReportRunRead",
    "ReportRunSummary",
    "ReportType",
    "StreamEvent",
]
