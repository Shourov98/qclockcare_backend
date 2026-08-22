"""Reports module — Claude streaming + export service.

The streaming path is request-scoped (the SPA's `EventSource` is open
for the lifetime of the request). We:

  1. Insert a `report_runs` row with `status='streaming'` so the SPA
     can store the `run_id` in the first frame and refer to it later.
  2. Aggregate data (sync DB query, no Claude involved).
  3. Stream Claude. As each `ContentBlockDeltaEvent` arrives we yield
     a `StreamEvent(kind='delta', delta=text)` to the SPA. We do NOT
     write to the DB on every token (a row-level UPDATE on every delta
     would saturate the connection pool) — instead we buffer and
     flush once at the end.
  4. On `MessageStopEvent`, finalize the row: status='completed',
     narrative=full text, input_tokens, output_tokens, cost_usd.
  5. On exception, finalize the row as `status='failed'`, error=...

The SSE wire format is `data: <json>\n\n` per frame. The frontend
parses each frame's JSON and dispatches on `kind`.

Export path (`get_artifact`) reads the persisted `aggregate_payload`
and renders it as PDF / CSV / XLSX. Re-running Claude is not needed.
"""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import Any, Literal

import anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.logging import get_logger
from src.modules.reports.aggregators import get_aggregator
from src.modules.reports.models import ReportRun
from src.modules.reports.prompts import SYSTEM_PROMPT, build_user_prompt
from src.modules.reports.schemas import ReportType, StreamEvent

logger = get_logger(__name__)

# Event types are checked via duck-typed attribute access rather than
# `isinstance(...)` against the SDK's `RawMessageStartEvent` etc. —
# the SDK's event classes are version-coupled and constructing real
# instances in tests is fragile. The contract the service relies on
# is just: the event has the right attribute names.
#
# In production these checks match `anthropic.types.RawMessageStartEvent`,
# `RawContentBlockDeltaEvent`, `RawMessageDeltaEvent`, `RawMessageStopEvent`
# (current SDK 0.121.x).


# --------------------------------------------------------------------------
# Anthropic pricing — used to compute cost_usd on a completed run.
# Keep in sync with https://www.anthropic.com/pricing. Sonnet 4.5 rates
# are USD per million tokens.
# --------------------------------------------------------------------------
_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 0.80, "output": 4.0},
    "claude-opus-4-7": {"input": 15.0, "output": 75.0},
}
_DEFAULT_PRICING: dict[str, float] = {"input": 3.0, "output": 15.0}


def _compute_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return the dollar cost of a Claude call rounded to 6 decimal places.

    Returns 0.0 for unknown models — the persisted row still has the
    token counts, so a future pricing-table update can backfill cost.
    """
    pricing = _PRICING.get(model, _DEFAULT_PRICING)
    cost = (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000
    return round(cost, 6)


# --------------------------------------------------------------------------
# ReportService.generate — streaming + persistence
# --------------------------------------------------------------------------
class ReportService:
    """Stream a Claude narrative and persist the run.

    One instance per request — the SDK's `AsyncAnthropic` holds an
    HTTP connection that we want to close as soon as the stream ends.
    """

    def __init__(self) -> None:
        self._client: anthropic.AsyncAnthropic | None = None

    def _ensure_client(self) -> anthropic.AsyncAnthropic:
        """Lazy-construct the Anthropic client from settings.

        Pulls the API key from `settings.CLAUDE_API_KEY` at first use
        rather than at construction time so callers can mock `settings`
        before injecting (handy in tests).
        """
        if self._client is None:
            key = settings.CLAUDE_API_KEY
            if key is None:
                raise RuntimeError("CLAUDE_API_KEY not set")
            self._client = anthropic.AsyncAnthropic(
                api_key=key.get_secret_value(),
                timeout=settings.CLAUDE_API_TIMEOUT_SECONDS,
            )
        return self._client

    async def generate(
        self,
        session: AsyncSession,
        *,
        agency_id: uuid.UUID,
        user_id: uuid.UUID | None,
        report_type: ReportType,
        params: Mapping[str, Any],
    ) -> AsyncIterator[StreamEvent]:
        """Aggregate data, stream Claude, persist the run.

        Yields `StreamEvent`s synchronously to the FastAPI `StreamingResponse`.
        The first event is `kind='run_meta'` with the freshly-inserted
        `run_id` so the SPA can store it before the narrative starts.
        Subsequent events are `kind='delta'` chunks. The final event
        is `kind='final'` with total tokens + cost.
        """
        # 1. Aggregate data BEFORE inserting the row — if the aggregator
        # crashes we don't end up with a half-initialised run.
        aggregator = get_aggregator(report_type.value)
        aggregate: dict[str, Any] = await aggregator(
            session, agency_id=agency_id, params=dict(params)
        )

        # 2. Insert the row.
        run = ReportRun(
            agency_id=agency_id,
            requested_by_user_id=user_id,
            report_type=report_type.value,
            params=dict(params),
            aggregate_payload=aggregate,
            status="streaming",
            claude_model=settings.CLAUDE_MODEL,
        )
        session.add(run)
        await session.flush()
        run_id = run.id

        # 3. Yield the run_meta frame BEFORE we start streaming text —
        # the SPA needs the id to enable the export buttons.
        yield StreamEvent(kind="run_meta", run_id=run_id)

        # 4. Call Claude. Stream events.
        user_prompt = build_user_prompt(report_type.value, params, aggregate)
        client = self._ensure_client()
        input_tokens = 0
        output_tokens = 0
        narrative_chunks: list[str] = []
        try:
            async with client.messages.stream(
                model=settings.CLAUDE_MODEL,
                max_tokens=settings.CLAUDE_MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            ) as stream:
                async for event in stream:
                    # Duck-typed dispatch — see the note at the top of
                    # this module. The SDK's event classes carry these
                    # attributes consistently across minor versions.
                    if hasattr(event, "message") and hasattr(event.message, "usage"):
                        # MessageStartEvent — input_tokens from the initial
                        # usage block.
                        usage = event.message.usage
                        if usage is not None:
                            input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
                    elif hasattr(event, "delta") and getattr(event.delta, "type", None) == "text_delta":
                        # ContentBlockDeltaEvent — text chunk.
                        text = event.delta.text
                        if text:
                            narrative_chunks.append(text)
                            yield StreamEvent(kind="delta", delta=text)
                    elif hasattr(event, "usage"):
                        # MessageDeltaEvent — output_tokens from the
                        # accumulated usage.
                        usage = event.usage
                        if usage is not None:
                            output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
                    # MessageStopEvent and others carry no actionable fields here.
        except Exception as exc:
            # Outermost catch — any SDK / network / timeout error
            # translates to a `failed` row and a `kind='error'` SSE frame.
            logger.error(
                "reports.stream_failed",
                run_id=str(run_id),
                report_type=report_type.value,
                error=type(exc).__name__,
                detail=str(exc),
            )
            run.status = "failed"
            run.error = f"{type(exc).__name__}: {exc}"[:1000]
            run.completed_at = datetime.now(UTC)
            await session.commit()
            yield StreamEvent(
                kind="error",
                error=f"Claude request failed: {type(exc).__name__}",
            )
            return

        # 5. Finalize the row.
        narrative = "".join(narrative_chunks)
        model = settings.CLAUDE_MODEL
        cost = _compute_cost_usd(model, input_tokens, output_tokens)
        run.narrative = narrative
        run.status = "completed"
        run.completed_at = datetime.now(UTC)
        run.input_tokens = input_tokens
        run.output_tokens = output_tokens
        run.cost_usd = cost
        await session.commit()

        logger.info(
            "reports.stream_completed",
            run_id=str(run_id),
            report_type=report_type.value,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            chars=len(narrative),
        )

        yield StreamEvent(
            kind="final",
            total_tokens=input_tokens + output_tokens,
            cost_usd=cost,
        )


# --------------------------------------------------------------------------
# Read endpoints
# --------------------------------------------------------------------------
async def list_runs(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    limit: int = 20,
) -> list[ReportRun]:
    """Most recent runs for the agency — feeds the history sidebar."""
    rows = await session.execute(
        select(ReportRun)
        .where(ReportRun.agency_id == agency_id)
        .order_by(ReportRun.created_at.desc())
        .limit(limit)
    )
    return list(rows.scalars())


async def get_run(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    agency_id: uuid.UUID,
) -> ReportRun | None:
    """One run by id, scoped to the agency.

    Returns None on miss — the router turns that into a 404.
    """
    return (
        await session.execute(
            select(ReportRun)
            .where(ReportRun.id == run_id)
            .where(ReportRun.agency_id == agency_id)
        )
    ).scalar_one_or_none()


# --------------------------------------------------------------------------
# Export — PDF / CSV / XLSX
# --------------------------------------------------------------------------
ExportFormat = Literal["pdf", "csv", "xlsx"]


def _flatten_aggregate_for_table(aggregate: Mapping[str, Any]) -> tuple[list[str], list[list[Any]]]:
    """Turn an aggregate snapshot into a flat table for CSV/XLSX.

    The aggregator returns a heterogeneous dict (e.g. `{"totals": {...},
    "by_status": {...}}` for visit_summary, `{"per_caregiver": [...]}` for
    staff). For CSV/XLSX we want a tabular layout — so we pick the first
    list-of-dicts we find and emit one row per entry. Falls back to a
    single-row key/value table if no list is present (e.g. compliance).

    Values are returned in their native Python types (not pre-stringified).
    The CSV renderer applies `_stringify_cell` per cell before writing; the
    XLSX renderer writes raw values so openpyxl can auto-type cells.
    """
    # Prefer the first list-of-dicts we find — that's the per-row table.
    for value in aggregate.values():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            headers = list(value[0].keys())
            rows = [[row.get(h) for h in headers] for row in value]
            return headers, rows

    # Fall back: a key/value table with scalars. Skip complex values
    # (dicts/lists) — they'd need pretty-printing that doesn't fit a
    # one-cell wide column. They're still available in the XLSX "_snapshot"
    # sheet as raw JSON.
    rows: list[list[Any]] = []
    for key, value in aggregate.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            rows.append([key, value])
    return ["key", "value"], rows


def _stringify_cell(value: Any) -> str:
    """Coerce a JSON-ish value into a displayable string.

    Dicts / lists pretty-print so a single CSV cell doesn't end up as
    `"{'a': 1, 'b': 2}"` (Python repr) — we'd rather see readable JSON.
    """
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    import orjson

    return orjson.dumps(value, option=orjson.OPT_INDENT_2).decode("utf-8")


def render_csv(aggregate: Mapping[str, Any]) -> bytes:
    """Render the snapshot as a CSV byte stream.

    Each cell is stringified via `_stringify_cell` so dicts/lists pretty-print
    as JSON instead of Python `repr`, and `None` becomes an empty field.
    """
    headers, rows = _flatten_aggregate_for_table(aggregate)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([_stringify_cell(h) for h in headers])
    for row in rows:
        writer.writerow([_stringify_cell(cell) for cell in row])
    return buf.getvalue().encode("utf-8")


def render_xlsx(aggregate: Mapping[str, Any]) -> bytes:
    """Render the snapshot as an .xlsx byte stream.

    The narrative sheet is skipped if there's no narrative yet (e.g. the
    user is exporting a still-streaming run — they'd see the snapshot
    only). The aggregate JSON is also written to a "_snapshot" sheet
    for debugging.
    """
    from openpyxl import Workbook

    headers, rows = _flatten_aggregate_for_table(aggregate)
    wb = Workbook()
    ws = wb.active
    ws.title = "Report"
    ws.append(headers)
    for row in rows:
        ws.append(row)

    # Debug sheet — the raw JSON snapshot. Useful when the report
    # looks wrong and the admin wants to see exactly what was sent
    # to Claude.
    import orjson

    snap_ws = wb.create_sheet("_snapshot")
    snap_ws.append(["key", "value"])
    for key, value in aggregate.items():
        snap_ws.append([key, orjson.dumps(value).decode("utf-8")])
    return _save_xlsx(wb)


def _save_xlsx(wb) -> bytes:
    """Persist the workbook to a BytesIO buffer."""
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def render_pdf(
    *,
    narrative: str | None,
    aggregate: Mapping[str, Any],
    report_type: str,
    agency_id: uuid.UUID,
) -> bytes:
    """Render the report as a one-page-equivalent PDF.

    Layout: title, narrative block, table of snapshot keys. We use
    `reportlab`'s `SimpleDocTemplate` with one `Story` block — for the
    complexity of the report types we keep layout simple on purpose.
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=LETTER,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        title=f"QlockCare Report — {report_type}",
    )
    styles = getSampleStyleSheet()
    body = styles["BodyText"]
    title = styles["Title"]
    h2 = styles["Heading2"]

    story: list[Any] = []
    story.append(Paragraph(f"QlockCare Report — {report_type}", title))
    story.append(
        Paragraph(
            f"Agency: {agency_id} • Generated: {datetime.now(UTC).isoformat()}",
            body,
        )
    )
    story.append(Spacer(1, 12))

    if narrative:
        # The narrative uses plain bullets (`-`) and paragraphs — strip
        # bullets we don't want reportlab to mis-render.
        lines = []
        for line in narrative.splitlines():
            if line.startswith("- "):
                lines.append(f"&bull; {line[2:]}")
            elif line.strip() == "":
                lines.append("<br/>")
            else:
                lines.append(line)
        story.append(Paragraph("<br/>".join(lines), body))
        story.append(Spacer(1, 12))

    story.append(Paragraph("Data snapshot", h2))
    headers, rows = _flatten_aggregate_for_table(aggregate)
    table = Table([headers, *rows], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("FONTSIZE", (0, 0), (-1, -1), 7),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(table)

    doc.build(story)
    return buf.getvalue()


def get_artifact(
    run: ReportRun,
    *,
    fmt: str,
) -> bytes:
    """Render the persisted run as a downloadable artifact.

    `run` must already have been loaded with the right agency scope.
    Export uses the persisted `aggregate_payload` — no re-query, no
    extra Claude call.
    """
    aggregate = run.aggregate_payload or {}
    if fmt == "csv":
        return render_csv(aggregate)
    if fmt == "xlsx":
        return render_xlsx(aggregate)
    if fmt == "pdf":
        return render_pdf(
            narrative=run.narrative,
            aggregate=aggregate,
            report_type=run.report_type,
            agency_id=run.agency_id,
        )
    raise ValueError(f"Unsupported export format: {fmt!r}")


__all__ = [
    "ExportFormat",
    "ReportService",
    "get_artifact",
    "get_run",
    "list_runs",
    "render_csv",
    "render_pdf",
    "render_xlsx",
]
